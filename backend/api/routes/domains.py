"""Un conjunto de endpoints por dominio de análisis: infraestructura, gobierno de datos y arquitectura.

    /api/v1/{dominio}/audits            POST   crea el trabajo (202)         GET /{id}  estado y resultado     DELETE /{id}  cancelar
    /api/v1/{dominio}/audit             POST   síncrono                      /audit/stream  NDJSON
    /api/v1/data-governance/audits/multipart  POST  diccionario como archivo
"""

import asyncio
import base64
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from backend.agent_graph.graph import DOMAIN_NODES
from backend.api.errors import ApiError
from backend.api.jobs import JobManager
from backend.api.schemas import (ERROR_RESPONSES, ArchitectureRequest, BaseRequest, DataGovernanceRequest,
                                 DomainJobStatus, DomainResult, InfrastructureRequest, JobCancelled, JobCreated)
from backend.api.security import authenticate
from backend.api.service import prepare

LLMKey = Header(default=None, alias="X-LLM-API-Key", description="Clave del proveedor de LLM (nunca va en el cuerpo).")

DOMAINS: dict[str, dict[str, Any]] = {
    "infrastructure": {"req": InfrastructureRequest, "tag": "Infraestructura",
                       "what": "seguridad (IAM, red, exposición), gobierno (cifrado, etiquetas, backups) y costos"},
    "data-governance": {"req": DataGovernanceRequest, "tag": "Gobierno de datos",
                        "what": "BigQuery/lake (capas medallion), flujos, capacidad de clusters y diccionario de datos"},
    "architecture": {"req": ArchitectureRequest, "tag": "Arquitectura",
                     "what": "componentes, flujos, identidades de ejecución y exposición pública"},
}


MAX_WAIT_S = 3300          # por debajo del tiempo máximo de una solicitud en Cloud Run (3600 s)
FINISHED = ("completed", "error", "cancelled")


def _status_payload(job: Any, domain: str, include_raw: bool) -> dict[str, Any]:
    result = job.result if job.status == "completed" else None
    if result is not None and not include_raw:
        result = {**result, "raw_infra_data": None}
    return {"job_id": job.id, "domain": domain, "status": job.status, "elapsed_s": job.elapsed_s,
            "events": job.events, "error": job.error, "result": result}


async def _create(request: Request, owner: str, req: BaseRequest, llm_key: str | None, domain: str) -> JobCreated:
    request.app.state.limiter.check(owner)
    state, config = await asyncio.to_thread(prepare, req, llm_key, request.app.state.settings, domain)
    job = request.app.state.jobs.create(owner, req, state, config)
    return JobCreated(job_id=job.id, status=job.status, domain=domain, nodes=DOMAIN_NODES[domain])


def make_router(domain: str) -> APIRouter:
    meta = DOMAINS[domain]
    Req = meta["req"]
    router = APIRouter(prefix=f"/api/v1/{domain}", tags=[meta["tag"]], responses=ERROR_RESPONSES)
    nodes = DOMAIN_NODES[domain]

    @router.post("/audits", status_code=202, response_model=JobCreated, name=f"create_{domain}",
                 responses={200: {"model": DomainJobStatus, "description": "Con `wait=true`: el análisis terminó."}},
                 summary=f"Crear análisis de {meta['tag'].lower()} en segundo plano",
                 description=f"Analiza: {meta['what']}. Responde 202 con `job_id`; consulte el avance con GET.")
    async def create(req: Req, request: Request, response: Response, owner: str = Depends(authenticate),  # type: ignore[valid-type]
                     x_llm_api_key: str | None = LLMKey,
                     wait: bool = Query(default=False, description="true: la llamada espera a que el análisis termine y devuelve "
                                                                   "el estado con el resultado (200). Si pasa `wait_timeout` sin terminar, "
                                                                   "responde 202 con el estado actual y se consulta con GET."),
                     wait_timeout: int = Query(default=MAX_WAIT_S, ge=1, le=MAX_WAIT_S, description="Segundos máximos de espera con wait=true."),
                     include_raw: bool = Query(default=False, description="Solo con wait=true: incluir raw_infra_data (varios MB).")) -> Any:
        created = await _create(request, owner, req, x_llm_api_key, domain)
        location = f"/api/v1/{domain}/audits/{created.job_id}"
        if not wait:
            response.headers["Location"] = location
            return created
        job = await request.app.state.jobs.wait(created.job_id, owner, wait_timeout, domain)
        return JSONResponse(_status_payload(job, domain, include_raw), headers={"Location": location},
                            status_code=200 if job.status in FINISHED else 202)

    @router.get("/audits/{job_id}", response_model=DomainJobStatus, name=f"get_{domain}",
                summary="Estado, eventos y resultado")
    async def get(job_id: str, request: Request, owner: str = Depends(authenticate),
                  include_raw: bool = Query(default=True, description="false omite raw_infra_data (varios MB)"),
                  wait: int = Query(default=0, ge=0, le=MAX_WAIT_S,
                                    description="Segundos que la llamada espera a que el trabajo termine antes de responder (0 = no espera).")) -> Any:
        job = await request.app.state.jobs.wait(job_id, owner, wait, domain)
        return _status_payload(job, domain, include_raw)

    @router.delete("/audits/{job_id}", response_model=JobCancelled, name=f"cancel_{domain}",
                   summary="Cancelar un trabajo en curso, o borrar el registro de uno terminado")
    async def cancel(job_id: str, request: Request, owner: str = Depends(authenticate)) -> JobCancelled:
        job = request.app.state.jobs.cancel(job_id, owner, domain)
        return JobCancelled(job_id=job.id, status=job.status)

    @router.post("/audit", response_model=DomainResult, name=f"run_{domain}",
                 summary="Ejecutar y esperar el resultado (conexión larga)")
    async def run(req: Req, request: Request, owner: str = Depends(authenticate),  # type: ignore[valid-type]
                  x_llm_api_key: str | None = LLMKey) -> Any:
        request.app.state.limiter.check(owner)
        state, config = await asyncio.to_thread(prepare, req, x_llm_api_key, request.app.state.settings, domain)
        mgr: JobManager = request.app.state.jobs
        async for ev in mgr.stream(req, state, config):
            if ev["event"] == "completed":
                return ev["result"]
            if ev["event"] == "error":
                raise ApiError(500, ev["detail"], "audit_failed")
        raise ApiError(500, "El análisis terminó sin resultado.", "audit_failed")

    @router.post("/audit/stream", name=f"stream_{domain}", response_class=StreamingResponse,
                 summary="Ejecutar con progreso en NDJSON (una línea JSON por evento)")
    async def stream(req: Req, request: Request, owner: str = Depends(authenticate),  # type: ignore[valid-type]
                     x_llm_api_key: str | None = LLMKey) -> StreamingResponse:
        request.app.state.limiter.check(owner)
        state, config = await asyncio.to_thread(prepare, req, x_llm_api_key, request.app.state.settings, domain)
        mgr: JobManager = request.app.state.jobs

        async def lines() -> AsyncIterator[str]:
            yield json.dumps({"event": "start", "domain": domain, "nodes": nodes}) + "\n"
            async for ev in mgr.stream(req, state, config):
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    if domain == "data-governance":
        @router.post("/audits/multipart", status_code=202, response_model=JobCreated, name="create_data_governance_multipart",
                     summary="Crear el análisis subiendo el diccionario como archivo (sin Base64)")
        async def multipart(request: Request, response: Response, owner: str = Depends(authenticate),
                            request_json: str = Form(alias="request", description="DataGovernanceRequest como JSON"),
                            dictionary: UploadFile | None = File(default=None),
                            x_llm_api_key: str | None = LLMKey) -> JobCreated:
            try:
                req = DataGovernanceRequest.model_validate_json(request_json)
            except ValidationError as exc:
                raise ApiError(422, [{"loc": ["request", *map(str, e["loc"])], "msg": e["msg"], "type": e["type"]}
                                     for e in exc.errors()], "validation_error") from exc
            if dictionary is not None:
                limit = request.app.state.settings.max_dictionary_bytes
                content = await dictionary.read(limit + 1)
                if len(content) > limit:
                    raise ApiError(400, "El diccionario supera el tamaño máximo permitido.", "invalid_dictionary")
                req = req.model_copy(update={"data_dictionary_b64": base64.b64encode(content).decode(),
                                             "data_dictionary_name": dictionary.filename or req.data_dictionary_name})
            created = await _create(request, owner, req, x_llm_api_key, domain)
            response.headers["Location"] = f"/api/v1/{domain}/audits/{created.job_id}"
            return created

    return router
