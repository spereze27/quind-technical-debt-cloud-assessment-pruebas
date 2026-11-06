"""Configuración del servicio. Todo se controla por variables de entorno (12-factor)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

API_VERSION = "1.7.0"


def _int(name: str, default: int, lo: int = 0) -> int:
    try:
        return max(lo, int(os.getenv(name, default)))
    except ValueError:
        return default


def _csv(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    version: str = API_VERSION
    # Seguridad
    api_keys: frozenset[str] = field(default_factory=frozenset)   # API_KEYS="clave1,clave2"
    require_auth: bool = False                                    # REQUIRE_AUTH=1 → no arranca sin API_KEYS
    cors_origins: tuple[str, ...] = ()    # CORS_ORIGINS: vacío = sin CORS (solo clientes de servidor/curl/Postman)
    max_body_mb: int = 20                                         # MAX_BODY_MB
    rate_limit_per_min: int = 20                                  # RATE_LIMIT_PER_MIN (creación de auditorías)
    # Ejecución
    max_concurrent: int = 2                                       # MAX_CONCURRENT_AUDITS
    max_queued: int = 20                                          # MAX_QUEUED_AUDITS (en cola + en curso)
    job_timeout_s: int = 1800                                     # JOB_TIMEOUT_SECONDS
    job_ttl_s: int = 3600                                         # JOB_TTL_SECONDS
    # Persistencia de trabajos
    job_store: str = "memory"                                     # JOB_STORE=memory|sqlite
    jobs_db: str = "jobs.sqlite3"                                 # JOBS_DB (solo con sqlite)
    max_dictionary_mb: int = 10

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def max_body_bytes(self) -> int:
        return self.max_body_mb * 1024 * 1024

    @property
    def max_dictionary_bytes(self) -> int:
        return self.max_dictionary_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            api_keys=frozenset(_csv("API_KEYS")),
            require_auth=os.getenv("REQUIRE_AUTH", "").lower() in ("1", "true", "yes"),
            cors_origins=tuple(_csv("CORS_ORIGINS")) or cls.cors_origins,
            max_body_mb=_int("MAX_BODY_MB", 20, 1),
            rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", 20, 0),
            max_concurrent=_int("MAX_CONCURRENT_AUDITS", 2, 1),
            max_queued=_int("MAX_QUEUED_AUDITS", 20, 1),
            job_timeout_s=_int("JOB_TIMEOUT_SECONDS", 1800, 30),
            job_ttl_s=_int("JOB_TTL_SECONDS", 3600, 60),
            job_store=os.getenv("JOB_STORE", "memory").lower(),
            jobs_db=os.getenv("JOBS_DB", "jobs.sqlite3"),
        )
        if s.job_store not in ("memory", "sqlite"):
            raise ValueError("JOB_STORE debe ser 'memory' o 'sqlite'")
        if s.require_auth and not s.api_keys:
            raise RuntimeError("REQUIRE_AUTH=1 pero API_KEYS está vacío: el servicio no arranca sin autenticación.")
        return s
