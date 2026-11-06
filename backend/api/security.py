"""Autenticación por API key propia (cabecera `X-API-Key`), distinta de la clave del LLM (`X-LLM-API-Key`)."""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

from fastapi import Request

from backend.api.errors import ApiError
from backend.api.settings import Settings

ANONYMOUS = "anonymous"


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def authenticate(request: Request) -> str:
    """Devuelve la identidad (huella de la clave). Sin API_KEYS configuradas, todo es `anonymous` (modo desarrollo)."""
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return ANONYMOUS
    supplied = request.headers.get("x-api-key", "")
    ok = False
    for key in settings.api_keys:                      # recorre todas: tiempo constante respecto a cuál coincide
        ok |= hmac.compare_digest(supplied.encode(), key.encode())
    if not ok:
        raise ApiError(401, "API key inválida o ausente (cabecera X-API-Key).", "unauthorized",
                       {"WWW-Authenticate": "ApiKey"})
    return fingerprint(supplied)


class RateLimiter:
    """Ventana deslizante de 60 s por identidad, en memoria (válido para un solo proceso)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, who: str) -> None:
        if self.per_minute <= 0:
            return
        now, q = time.monotonic(), self._hits[who]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            raise ApiError(429, "Demasiadas solicitudes: intente de nuevo en un minuto.", "rate_limited",
                           {"Retry-After": "60"})
        q.append(now)
