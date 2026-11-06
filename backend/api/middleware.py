"""Middleware ASGI: identificador de petición y límite de tamaño del cuerpo (también sin Content-Length)."""
from __future__ import annotations

import json
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from starlette.exceptions import HTTPException as StarletteHTTPException


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        rid = headers.get(b"x-request-id", b"").decode()[:64] or uuid.uuid4().hex[:16]
        if not all(c.isalnum() or c in "-_." for c in rid):    # evita inyección en cabeceras/logs
            rid = uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["request_id"] = rid

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        await self.app(scope, receive, send_with_id)


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return await self._reject(scope, send)
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise StarletteHTTPException(413, f"El cuerpo supera {self.max_bytes // (1024 * 1024)} MB.")   # FastAPI la deja pasar tal cual
            return message

        await self.app(scope, limited, send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        body: dict[str, Any] = {"detail": f"El cuerpo supera {self.max_bytes // (1024 * 1024)} MB.",
                                "code": "payload_too_large", "request_id": scope.get("state", {}).get("request_id")}
        raw = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]})
        await send({"type": "http.response.body", "body": raw})
