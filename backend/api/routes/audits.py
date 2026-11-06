"""Endpoints de auditoría: trabajos asíncronos (recomendado), ejecución síncrona y streaming NDJSON."""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from backend.agent_graph.graph import NODE_SEQUENCE
from backend.api.errors import ApiError
from backend.api.jobs import JobManager
from backend.api.schemas import (ERROR_RESPONSES, AuditRequest, AuditResult, JobCancelled, JobCreated, JobStatus)
from backend.api.security import RateLimiter, authenticate
from backend.api.service import graph_events, prepare

router = APIRouter(prefix="/api/v1", tags=["Auditorías"], responses=ERROR_RESPONSES)

LLMKey = Header(default=None, alias="X-LLM-API-Key", description="Clave del proveedor de LLM (nunca va en el cuerpo).")


def _manager(request: Request) -> JobManager:
    return request.app.state.jobs


def _limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


async def _create(request: Request, owner: str, req: AuditRequest, llm_key: str | None) -> JobCreated:
    _limiter(request).check(owner)
    state, config = await asyncio.to_thread(prepare, req, llm_key, request.app.state.settings)   # parseo pesado fuera del loop
    job = _manager(request).create(owner, req, state, config)
    return JobCreated(job_id=job.id, status=job.status, nodes=NODE_SEQUENCE)


@router.post("/audits", status_code=202, response_model=JobCreated, summary="Crear una auditoría en segundo plano")
async def create_audit(req: AuditRequest, request: Request, response: Response, owner: str = Depends(authenticate),
                       x_llm_api_key: str | None = LLMKey) -> JobCreated:
    created = await _create(request, owner, req, x_llm_api_key)
    response.headers["Location"] = f"/api/v1/audits/{created.job_id}"
    return created


@router.post("/audits/multipart", status_code=202, response_model=JobCreated,
             summary="Crear una auditoría subiendo el diccionario como archivo (sin Base64)")
async def create_audit_multipart(request: Request, response: Response, owner: str = Depends(authenticate),
                                 request_json: str = Form(alias="request", description="AuditRequest como texto JSON"),
                                 dictionary: UploadFile | None = File(default=None),
                                 x_llm_api_key: str | None = LLMKey) -> JobCreated:
    try:
        req = AuditRequest.model_validate_json(request_json)
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
    created = await _create(request, owner, req, x_llm_api_key)
    response.headers["Location"] = f"/api/v1/audits/{created.job_id}"
    return created


@router.get("/audits/{job_id}", response_model=JobStatus, summary="Estado, eventos y resultado de una auditoría")
async def get_audit(job_id: str, request: Request, owner: str = Depends(authenticate),
                    include_raw: bool = Query(default=True, description="false omite raw_infra_data (varios MB)")) -> Any:
    job = _manager(request).get(job_id, owner, "full")
    result = job.result if job.status == "completed" else None
    if result is not None and not include_raw:
        result = {**result, "raw_infra_data": None}
    return {"job_id": job.id, "status": job.status, "elapsed_s": job.elapsed_s, "events": job.events,
            "error": job.error, "result": result}


@router.delete("/audits/{job_id}", response_model=JobCancelled,
               summary="Cancelar una auditoría en curso, o borrar el registro de una terminada")
async def cancel_audit(job_id: str, request: Request, owner: str = Depends(authenticate)) -> JobCancelled:
    job = _manager(request).cancel(job_id, owner, "full")
    return JobCancelled(job_id=job.id, status=job.status)


# ------------------------------------------------------------------ síncronos (mantienen la conexión abierta)
@router.post("/audit", response_model=AuditResult, summary="Ejecutar y esperar el resultado (conexión larga)")
async def run_audit(req: AuditRequest, request: Request, owner: str = Depends(authenticate),
                    x_llm_api_key: str | None = LLMKey) -> Any:
    _limiter(request).check(owner)
    state, config = await asyncio.to_thread(prepare, req, x_llm_api_key, request.app.state.settings)
    mgr: JobManager = _manager(request)
    async for ev in mgr.stream(req, state, config):
        if ev["event"] == "completed":
            return ev["result"]
        if ev["event"] == "error":
            raise ApiError(500, ev["detail"], "audit_failed")
    raise ApiError(500, "La auditoría terminó sin resultado.", "audit_failed")


@router.post("/audit/stream", summary="Ejecutar con progreso en NDJSON (una línea JSON por evento)",
             response_class=StreamingResponse)
async def run_audit_stream(req: AuditRequest, request: Request, owner: str = Depends(authenticate),
                           x_llm_api_key: str | None = LLMKey) -> StreamingResponse:
    _limiter(request).check(owner)
    state, config = await asyncio.to_thread(prepare, req, x_llm_api_key, request.app.state.settings)
    mgr: JobManager = _manager(request)

    async def lines() -> AsyncIterator[str]:
        yield json.dumps({"event": "start", "nodes": NODE_SEQUENCE}) + "\n"
        async for ev in mgr.stream(req, state, config):
            yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")
