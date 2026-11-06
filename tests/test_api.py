"""Tests de la capa API: seguridad, trabajos (cola, cancelación, persistencia), límites y saneamiento de errores."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.agent_graph import graph as graph_mod
from backend.agent_graph.graph import NODE_SEQUENCE
from backend.agent_graph.nodes import base, report
from backend.api import jobs as jobs_mod
from backend.api.app import create_app
from backend.api.settings import Settings
from tests.test_graph import FakeLLM

LLM = {"X-LLM-API-Key": "sk-test-secret-123"}
BODY = {"provider": "AWS", "scope": "project", "target_id": "123456789012"}


@pytest.fixture(autouse=True)
def _fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOVERY_MODE", "mock")
    monkeypatch.setattr(base, "get_llm_from_config", lambda cfg: FakeLLM())
    monkeypatch.setattr(report, "get_llm_from_config", lambda cfg: FakeLLM())
    graph_mod.get_compiled_graph.cache_clear()


def make(**kw: Any) -> TestClient:
    return TestClient(create_app(Settings(**kw)))


def wait(client: TestClient, jid: str, headers: dict | None = None, until=("completed", "error", "cancelled")) -> dict:
    for _ in range(100):
        j = client.get(f"/api/v1/audits/{jid}", headers=headers or {}).json()
        if j["status"] in until:
            return j
        time.sleep(0.05)
    raise AssertionError(f"el trabajo no llegó a {until}: {j['status']}")


def slow_events(delay: float = 30.0):
    async def gen(req, state, config):
        yield {"event": "node_completed", "node": "discovery", "summary": {"findings": 0, "errors": []}}
        await asyncio.sleep(delay)
        yield {"event": "completed", "result": {"provider": "AWS", "findings": [], "kpis": {}, "errors": []}}
    return gen


# ------------------------------------------------------------------ seguridad
def test_auth_required_when_keys_configured() -> None:
    with make(api_keys=frozenset({"k1"})) as c:
        assert c.get("/health").json()["auth_enabled"] is True           # /health siempre abierto
        assert c.post("/api/v1/audits", json=BODY, headers=LLM).status_code == 401
        assert c.post("/api/v1/audits", json=BODY, headers={**LLM, "X-API-Key": "mala"}).status_code == 401
        r = c.post("/api/v1/audits", json=BODY, headers={**LLM, "X-API-Key": "k1"})
        assert r.status_code == 202 and r.headers["location"].endswith(r.json()["job_id"])
        assert c.get("/api/v1/reports/" + "a" * 32 + ".xlsx").status_code == 401


def test_jobs_are_isolated_per_api_key() -> None:
    with make(api_keys=frozenset({"ka", "kb"})) as c:
        a = {"X-API-Key": "ka"}
        jid = c.post("/api/v1/audits", json=BODY, headers={**LLM, **a}).json()["job_id"]
        assert wait(c, jid, a)["status"] == "completed"
        assert c.get(f"/api/v1/audits/{jid}", headers={"X-API-Key": "kb"}).status_code == 404
        assert c.delete(f"/api/v1/audits/{jid}", headers={"X-API-Key": "kb"}).status_code == 404


def test_require_auth_without_keys_fails_fast() -> None:
    with pytest.raises(RuntimeError):
        monkey = pytest.MonkeyPatch()
        monkey.setenv("REQUIRE_AUTH", "1")
        monkey.delenv("API_KEYS", raising=False)
        try:
            Settings.from_env()
        finally:
            monkey.undo()


def test_cors_only_allowed_origins() -> None:
    with make(cors_origins=("https://app.empresa.com",)) as c:
        ok = c.options("/api/v1/audits", headers={"Origin": "https://app.empresa.com",
                                                   "Access-Control-Request-Method": "POST"})
        bad = c.options("/api/v1/audits", headers={"Origin": "https://evil.com", "Access-Control-Request-Method": "POST"})
        assert ok.headers.get("access-control-allow-origin") == "https://app.empresa.com"
        assert "access-control-allow-origin" not in bad.headers


def test_rate_limit() -> None:
    with make(rate_limit_per_min=2) as c:
        codes = [c.post("/api/v1/audits", json=BODY, headers=LLM).status_code for _ in range(3)]
        assert codes == [202, 202, 429]


# ------------------------------------------------------------------ contrato y errores
def test_job_result_matches_contract_and_hides_secrets() -> None:
    with make() as c:
        jid = c.post("/api/v1/audits", json=BODY, headers=LLM).json()["job_id"]
        j = wait(c, jid)
        assert j["status"] == "completed" and len(j["events"]) == len(NODE_SEQUENCE)
        res = j["result"]
        assert {"findings", "kpis", "final_report", "data_report", "excel_report", "errors", "raw_infra_data",
                "data_profile", "dictionary_report", "profile_excel"} <= set(res)
        assert res["data_profile"] is None and res["excel_report"]["report_id"]
        assert "path" not in res["excel_report"] and "sk-test-secret-123" not in json.dumps(j)
        lite = c.get(f"/api/v1/audits/{jid}?include_raw=false").json()
        assert lite["result"]["raw_infra_data"] is None and lite["result"]["kpis"]
        assert c.get(f"/api/v1/reports/{res['excel_report']['report_id']}.xlsx").status_code == 200


def test_openapi_documents_models() -> None:
    with make() as c:
        spec = c.get("/openapi.json").json()
        assert "/api/v1/audits/{job_id}" in spec["paths"] and "delete" in spec["paths"]["/api/v1/audits/{job_id}"]
        assert {"AuditRequest", "JobStatus", "AuditResult", "Finding", "ErrorResponse"} <= set(spec["components"]["schemas"])


def test_validation_error_does_not_echo_input() -> None:
    with make() as c:
        r = c.post("/api/v1/audits", json={**BODY, "llm_provider": "VALOR-SECRETO-XYZ", "extra_field": 1}, headers=LLM)
        assert r.status_code == 422 and r.json()["code"] == "validation_error"
        assert "VALOR-SECRETO-XYZ" not in r.text and "request_id" in r.json()


def test_request_id_is_returned_and_sanitized() -> None:
    with make() as c:
        assert c.get("/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
        assert c.get("/health", headers={"X-Request-ID": "mal\tid"}).headers["x-request-id"] != "mal\tid"


@pytest.mark.parametrize("creds", ["no es json", "[1, 2]", json.dumps({"type": "authorized_user"})])
def test_invalid_credentials_are_400(creds: str) -> None:
    with make() as c:
        r = c.post("/api/v1/audits", json={**BODY, "cloud_credentials": creds}, headers=LLM)
        assert r.status_code == 400 and r.json()["code"] == "invalid_credentials"


def test_missing_llm_key_and_bad_catalog() -> None:
    with make() as c:
        assert c.post("/api/v1/audits", json=BODY).status_code == 401
        assert c.post("/api/v1/audits", json={**BODY, "flow_catalog": "- 1\n- 2"}, headers=LLM).status_code == 400


def test_dictionary_validation_and_missing_module() -> None:
    with make() as c:
        r = c.post("/api/v1/audits", json={**BODY, "data_dictionary_b64": "%%%no-base64%%%"}, headers=LLM)
        assert r.status_code == 400 and r.json()["code"] == "invalid_dictionary"
        ok_b64 = base64.b64encode(b"a,b\n1,2").decode()
        r = c.post("/api/v1/audits", json={**BODY, "data_dictionary_b64": ok_b64, "data_dictionary_name": "d.csv"},
                   headers=LLM)
        assert r.status_code in (202, 501)       # 501 si el módulo v1.5 no está instalado; 400/202 si lo está


def test_multipart_endpoint() -> None:
    with make() as c:
        r = c.post("/api/v1/audits/multipart", data={"request": json.dumps(BODY)}, headers=LLM)
        assert r.status_code == 202
        r = c.post("/api/v1/audits/multipart", data={"request": "{no json"}, headers=LLM)
        assert r.status_code == 422
        r = c.post("/api/v1/audits/multipart", data={"request": json.dumps(BODY)},
                   files={"dictionary": ("d.csv", b"a,b\n1,2", "text/csv")}, headers=LLM)
        assert r.status_code in (202, 501, 400)


def test_body_limit() -> None:
    with make(max_body_mb=1) as c:
        r = c.post("/api/v1/audits", content=b"x" * (2 * 1024 * 1024), headers={**LLM, "Content-Type": "application/json"})
        assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


def test_body_limit_without_content_length() -> None:
    def chunks():
        for _ in range(3):
            yield b"x" * (600 * 1024)

    with make(max_body_mb=1) as c:
        r = c.post("/api/v1/audits", content=chunks(), headers={**LLM, "Content-Type": "application/json"})
        assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


def test_sync_endpoints_still_work_with_auth_and_scrub_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    with make(api_keys=frozenset({"k1"})) as c:
        h = {**LLM, "X-API-Key": "k1"}
        r = c.post("/api/v1/audit", json=BODY, headers=h)
        assert r.status_code == 200 and r.json()["final_report"]

        async def boom(req, state, config):
            raise RuntimeError("fallo con la clave sk-test-secret-123 dentro")
            yield  # pragma: no cover

        monkeypatch.setattr(jobs_mod, "graph_events", boom)
        jid = c.post("/api/v1/audits", json=BODY, headers=h).json()["job_id"]
        j = wait(c, jid, {"X-API-Key": "k1"})
        assert j["status"] == "error" and "sk-test-secret-123" not in json.dumps(j)


# ------------------------------------------------------------------ cola, cancelación y límites
def test_cancel_running_then_delete_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs_mod, "graph_events", slow_events())
    with make() as c:
        jid = c.post("/api/v1/audits", json=BODY, headers=LLM).json()["job_id"]
        wait(c, jid, until=("running",))
        assert c.delete(f"/api/v1/audits/{jid}").status_code == 200
        assert wait(c, jid)["status"] == "cancelled"
        assert c.delete(f"/api/v1/audits/{jid}").status_code == 200          # terminada → borra el registro
        assert c.get(f"/api/v1/audits/{jid}").status_code == 404


def test_queue_concurrency_and_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs_mod, "graph_events", slow_events())
    with make(max_concurrent=1, max_queued=2, rate_limit_per_min=0) as c:
        a = c.post("/api/v1/audits", json=BODY, headers=LLM).json()["job_id"]
        b = c.post("/api/v1/audits", json=BODY, headers=LLM).json()["job_id"]
        wait(c, a, until=("running",))
        assert c.get(f"/api/v1/audits/{b}").json()["status"] == "queued"      # espera turno
        r = c.post("/api/v1/audits", json=BODY, headers=LLM)
        assert r.status_code == 429 and r.json()["code"] == "queue_full" and r.headers["retry-after"]
        assert c.get("/health").json()["active_jobs"] == 2


def test_job_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs_mod, "graph_events", slow_events())
    s = Settings(job_timeout_s=30)
    object.__setattr__(s, "job_timeout_s", 1)           # el mínimo de la configuración real es 30 s
    with TestClient(create_app(s)) as c:
        jid = c.post("/api/v1/audits", json=BODY, headers=LLM).json()["job_id"]
        j = wait(c, jid)
        assert j["status"] == "error" and "Tiempo máximo" in j["error"]


# ------------------------------------------------------------------ persistencia
def test_sqlite_store_survives_restart_and_interrupts_active(tmp_path) -> None:
    db = str(tmp_path / "jobs.sqlite3")
    with make(job_store="sqlite", jobs_db=db) as c:
        jid = c.post("/api/v1/audits", json=BODY, headers=LLM).json()["job_id"]
        assert wait(c, jid)["status"] == "completed"
    import os
    assert oct(os.stat(db).st_mode)[-3:] == "600"
    with make(job_store="sqlite", jobs_db=db) as c2:                      # "reinicio"
        j = c2.get(f"/api/v1/audits/{jid}").json()
        assert j["status"] == "completed" and j["result"]["final_report"]
    store = jobs_mod.SqliteStore(db)
    stale = jobs_mod.Job("b" * 32, "anonymous", status="running")
    store.save(stale)
    with make(job_store="sqlite", jobs_db=db) as c3:
        j = c3.get(f"/api/v1/audits/{'b' * 32}").json()
        assert j["status"] == "error" and "reinicio" in j["error"]


def test_stale_credential_files_are_removed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import tempfile

    from backend.api.service import cleanup_stale_credentials

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    old, new = tmp_path / "sa_old.json", tmp_path / "sa_new.json"
    old.write_text("{}"); new.write_text("{}")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert cleanup_stale_credentials(3600) == 1 and not old.exists() and new.exists()
