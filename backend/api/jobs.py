"""Trabajos en segundo plano: cola con concurrencia limitada, tiempo máximo, cancelación y persistencia opcional.

Estados: queued → running → completed | error | cancelled.
Cada trabajo pertenece a la identidad (huella de API key) que lo creó; las demás identidades reciben 404.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.agent_graph.graph import NODE_SEQUENCE
from backend.api.errors import ApiError
from backend.api.schemas import BaseRequest
from backend.api.service import graph_events
from backend.api.settings import Settings

logger = logging.getLogger("api")
MAX_EVENTS = 200
ACTIVE = ("queued", "running")


@dataclass
class Job:
    id: str
    owner: str
    status: str = "queued"
    created: float = field(default_factory=time.time)
    finished: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    domain: str = "full"

    @property
    def elapsed_s(self) -> float:
        return round((self.finished or time.time()) - self.created, 1)


class JobStore(Protocol):
    def save(self, job: Job) -> None: ...
    def get(self, job_id: str) -> Job | None: ...
    def delete(self, job_id: str) -> None: ...
    def purge(self, older_than: float) -> int: ...
    def interrupt_active(self) -> int: ...


class MemoryStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def save(self, job: Job) -> None:
        self._jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def purge(self, older_than: float) -> int:
        old = [j for j, v in self._jobs.items() if v.created < older_than and v.status not in ACTIVE]
        for j in old:
            del self._jobs[j]
        return len(old)

    def interrupt_active(self) -> int:
        return 0


class SqliteStore:
    """Persistencia en un archivo SQLite. OJO: el resultado contiene el inventario de la nube auditada;
    el archivo se crea con permisos 0600 y debe ubicarse en un disco cifrado."""

    def __init__(self, path: str) -> None:
        self.path, self._lock = path, threading.Lock()
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, status TEXT, created REAL,"
                      " finished REAL, events TEXT, result TEXT, error TEXT, domain TEXT DEFAULT 'full')")
            try:
                c.execute("ALTER TABLE jobs ADD COLUMN domain TEXT DEFAULT 'full'")   # migra bases de la v1.6
            except sqlite3.OperationalError:
                pass

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def save(self, job: Job) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO jobs (id,owner,status,created,finished,events,result,error,domain) VALUES (?,?,?,?,?,?,?,?,?)",
                      (job.id, job.owner, job.status, job.created, job.finished, json.dumps(job.events),
                       json.dumps(job.result, default=str) if job.result is not None else None, job.error, job.domain))

    def get(self, job_id: str) -> Job | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT id,owner,status,created,finished,events,result,error,domain FROM jobs WHERE id=?",
                            (job_id,)).fetchone()
        if not row:
            return None
        return Job(row[0], row[1], row[2], row[3], row[4], json.loads(row[5] or "[]"),
                   json.loads(row[6]) if row[6] else None, row[7], row[8] or "full")

    def delete(self, job_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def purge(self, older_than: float) -> int:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM jobs WHERE created<? AND status NOT IN ('queued','running')",
                             (older_than,)).rowcount

    def interrupt_active(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute("UPDATE jobs SET status='error', finished=?, error=? WHERE status IN ('queued','running')",
                             (time.time(), "Interrumpida por reinicio del servicio.")).rowcount


def make_store(settings: Settings) -> JobStore:
    return SqliteStore(settings.jobs_db) if settings.job_store == "sqlite" else MemoryStore()


class JobManager:
    def __init__(self, settings: Settings, store: JobStore | None = None) -> None:
        self.settings = settings
        self.store = store or make_store(settings)
        self._sem = asyncio.Semaphore(settings.max_concurrent)
        self._tasks: dict[str, asyncio.Task] = {}
        self._inline = 0

    # ---- ciclo de vida
    def startup(self) -> None:
        n = self.store.interrupt_active()
        if n:
            logger.warning("%s trabajos que estaban activos se marcaron como interrumpidos", n)

    async def shutdown(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    @property
    def active(self) -> int:
        return len(self._tasks) + self._inline

    def purge(self) -> None:
        self.store.purge(time.time() - self.settings.job_ttl_s)

    # ---- API
    def create(self, owner: str, req: BaseRequest, state: dict[str, Any], config: dict[str, Any]) -> Job:
        if self.active >= self.settings.max_queued:
            raise ApiError(429, "Hay demasiadas auditorías en cola o en curso; intente más tarde.", "queue_full",
                           {"Retry-After": "60"})
        self.purge()
        job = Job(uuid.uuid4().hex, owner, domain=state.get("domain", "full"))
        self.store.save(job)
        task = asyncio.create_task(self._run(job, req, state, config))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: self._tasks.pop(jid, None))
        return job

    def get(self, job_id: str, owner: str, domain: str | None = None) -> Job:
        job = self.store.get(job_id) if len(job_id) == 32 else None
        if job is None or job.owner != owner or (domain is not None and job.domain != domain):          # otra identidad = como si no existiera
            raise ApiError(404, "Auditoría no encontrada, vencida o el servicio se reinició.", "not_found")
        return job

    def cancel(self, job_id: str, owner: str, domain: str | None = None) -> Job:
        job = self.get(job_id, owner, domain)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()                                # la tarea marca el estado `cancelled` al terminar
        elif job.status not in ACTIVE:
            self.store.delete(job_id)                    # ya terminó: DELETE equivale a borrar el registro
        return job

    # ---- ejecución síncrona (endpoints /audit y /audit/stream): comparte cola y límites con los trabajos
    async def stream(self, req: BaseRequest, state: dict[str, Any], config: dict[str, Any]):
        if self.active >= self.settings.max_queued:
            raise ApiError(429, "Hay demasiadas auditorías en cola o en curso; intente más tarde.", "queue_full",
                           {"Retry-After": "60"})
        self._inline += 1
        try:
            async with self._sem:
                try:
                    async with asyncio.timeout(self.settings.job_timeout_s):
                        async for ev in graph_events(req, state, config):
                            yield ev
                except TimeoutError:
                    yield {"event": "error", "detail": f"Tiempo máximo excedido ({self.settings.job_timeout_s} s)."}
        finally:
            self._inline -= 1

    # ---- ejecución
    async def _run(self, job: Job, req: BaseRequest, state: dict[str, Any], config: dict[str, Any]) -> None:
        try:
            async with self._sem:                        # espera turno en estado `queued`
                job.status = "running"
                self.store.save(job)
                async with asyncio.timeout(self.settings.job_timeout_s):
                    async for ev in graph_events(req, state, config):
                        if ev["event"] == "completed":
                            job.result, job.status = ev["result"], "completed"
                        elif ev["event"] == "error":
                            job.error, job.status = ev["detail"], "error"
                        elif len(job.events) < MAX_EVENTS:
                            job.events.append({"event": "node_completed", "node": ev["node"], "summary": ev["summary"]})
                            if job.status == "running":
                                self.store.save(job)
        except asyncio.CancelledError:
            job.status, job.error = "cancelled", "Cancelada por el usuario."
        except TimeoutError:
            job.status, job.error = "error", f"Tiempo máximo excedido ({self.settings.job_timeout_s} s)."
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo del ejecutor de trabajos")
            job.status, job.error = "error", f"{type(exc).__name__}"
        finally:
            if job.status in ACTIVE:                     # el generador terminó sin evento final
                job.status, job.error = "error", job.error or "La auditoría terminó sin resultado."
            job.finished = time.time()
            try:
                await asyncio.to_thread(self.store.save, job)
            except Exception:  # noqa: BLE001
                logger.exception("No se pudo guardar el trabajo %s", job.id)


__all__ = ["Job", "JobManager", "MemoryStore", "SqliteStore", "NODE_SEQUENCE"]
