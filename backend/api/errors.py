"""Errores de la API: formato único `{detail, code, request_id}` y SIN eco de datos sensibles.

FastAPI, por defecto, devuelve en los 422 el valor enviado (`input`): con `cloud_credentials` o el diccionario en
Base64 eso filtraría secretos a logs y proxies. Aquí se elimina.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("api")


class ApiError(Exception):
    def __init__(self, status: int, detail: str, code: str = "error", headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.code, self.headers = status, detail, code, headers or {}


def _body(request: Request, status: int, detail: Any, code: str) -> dict[str, Any]:
    return {"detail": detail, "code": code, "request_id": getattr(request.state, "request_id", None)}


def scrub(text: str, secrets: list[str | None]) -> str:
    """Quita de un mensaje de error cualquier secreto conocido (API key del LLM, clave privada de la SA)."""
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return text


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(request, exc.status, exc.detail, exc.code), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 413: "payload_too_large"}.get(exc.status_code, "http_error")
        return JSONResponse(_body(request, exc.status_code, exc.detail, code), status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}   # sin `input`
                  for e in exc.errors()]
        return JSONResponse(_body(request, 422, errors, "validation_error"), status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Error no controlado [%s]", getattr(request.state, "request_id", "-"))
        return JSONResponse(_body(request, 500, "Error interno del servidor.", "internal_error"), status_code=500)


__all__ = ["ApiError", "HTTPException", "install", "scrub"]
