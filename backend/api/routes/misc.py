"""Salud y descarga de informes Excel."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from backend.agent_graph.graph import DOMAIN_NODES, NODE_SEQUENCE
from backend.agent_graph.nodes.discovery import missing_gcp_modules
from backend.agent_graph.nodes.excel_export import report_path
from backend.api.errors import ApiError
from backend.api.schemas import ERROR_RESPONSES, Health
from backend.api.security import authenticate

router = APIRouter(responses=ERROR_RESPONSES)


@router.get("/health", response_model=Health, tags=["Sistema"], summary="Estado del servicio (sin autenticación)")
async def health(request: Request) -> Health:
    s = request.app.state.settings
    return Health(status="ok", version=s.version, nodes=NODE_SEQUENCE, domains=DOMAIN_NODES,
                  discovery_mode=os.getenv("DISCOVERY_MODE", "real").lower(),
                  missing_gcp_modules=missing_gcp_modules(), auth_enabled=s.auth_enabled, job_store=s.job_store,
                  active_jobs=request.app.state.jobs.active, max_concurrent=s.max_concurrent)


@router.get("/api/v1/reports/{report_id}.xlsx", tags=["Informes"], summary="Descargar un Excel generado (vigente 24 h)",
            response_class=FileResponse, dependencies=[Depends(authenticate)])
async def download_excel(report_id: str) -> FileResponse:
    path = report_path(report_id)
    if path is None:
        raise ApiError(404, "Informe no encontrado o expirado (se conservan 24 h).", "not_found")
    return FileResponse(path, filename=f"informe_{report_id[:8]}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

