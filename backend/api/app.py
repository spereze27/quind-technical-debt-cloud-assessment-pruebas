"""Fábrica de la aplicación FastAPI (`create_app`). `backend.main:app` es la instancia con la configuración del entorno."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.agent_graph.nodes.discovery import missing_gcp_modules
from backend.api import errors
from backend.api.jobs import JobManager
from backend.api.middleware import BodyLimitMiddleware, RequestIdMiddleware
from backend.api.routes import audits, domains, misc
from backend.api.security import RateLimiter
from backend.api.service import cleanup_stale_credentials
from backend.api.settings import Settings

logger = logging.getLogger("api")

DESCRIPTION = """
API REST del analizador de deuda técnica cloud.

**Endpoints por dominio:** `/api/v1/infrastructure`, `/api/v1/data-governance`, `/api/v1/architecture` (cada uno con `/audits`, `/audit` y `/audit/stream`).

**Flujo recomendado:** `POST /api/v1/{dominio}/audits` → `GET /api/v1/audits/{job_id}` cada 2 s → `GET /api/v1/reports/{report_id}.xlsx`.

**Cabeceras:** `X-API-Key` (autenticación del servicio, si está configurada) y `X-LLM-API-Key` (clave del proveedor de LLM).
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.jobs.startup()
        if n := cleanup_stale_credentials():
            logger.warning("Se borraron %s archivos temporales de credenciales huérfanos", n)
        if not settings.auth_enabled:
            logger.warning("API SIN AUTENTICACIÓN: defina API_KEYS (y REQUIRE_AUTH=1) antes de exponerla fuera de localhost.")
        if "*" in settings.cors_origins:
            logger.warning("CORS abierto a cualquier origen (CORS_ORIGINS=*).")
        if missing := missing_gcp_modules():
            logger.warning("Faltan librerías de GCP en %s: %s", sys.executable, missing)

        async def janitor() -> None:
            while True:
                await asyncio.sleep(300)
                app.state.jobs.purge()

        task = asyncio.create_task(janitor())
        try:
            yield
        finally:
            task.cancel()
            await app.state.jobs.shutdown()

    app = FastAPI(title="Cloud Tech Debt Analyzer", version=settings.version, description=DESCRIPTION, lifespan=lifespan)
    app.state.settings = settings
    app.state.jobs = JobManager(settings)
    app.state.limiter = RateLimiter(settings.rate_limit_per_min)

    errors.install(app)
    wildcard = "*" in settings.cors_origins
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=not wildcard,
                       allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                       allow_headers=["Content-Type", "X-API-Key", "X-LLM-API-Key", "X-Request-ID"],
                       expose_headers=["X-Request-ID", "Location", "Retry-After"])
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(misc.router)
    app.include_router(audits.router)
    for name in domains.DOMAINS:
        app.include_router(domains.make_router(name))
    return app
