"""Tests de los endpoints por dominio: aislamiento, contrato, KPIs de dominio y análisis de arquitectura."""
from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.agent_graph import graph as graph_mod
from backend.agent_graph.graph import DOMAIN_NODES, DOMAINS
from backend.agent_graph.nodes import base, domains as dnodes, report
from backend.api.app import create_app
from backend.api.settings import Settings
from backend.core.architecture_analysis import analyze_architecture
from backend.core.domain_kpis import compute_domain_kpis
from tests.test_graph import FakeLLM

LLM = {"X-LLM-API-Key": "sk-test-secret-123"}
BODY = {"provider": "AWS", "scope": "project", "target_id": "123456789012"}


@pytest.fixture(autouse=True)
def _fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOVERY_MODE", "mock")
    for m in (base, report, dnodes):
        monkeypatch.setattr(m, "get_llm_from_config", lambda cfg: FakeLLM())
    graph_mod.get_graph_for.cache_clear()
    graph_mod.get_compiled_graph.cache_clear()


def wait(c: TestClient, domain: str, jid: str, headers: dict | None = None) -> dict:
    for _ in range(100):
        j = c.get(f"/api/v1/{domain}/audits/{jid}", headers=headers or {}).json()
        if j["status"] in ("completed", "error", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError(j)


# ---- arquitectura sintética (el modo mock no trae grafo)
def arch_raw() -> dict[str, Any]:
    n = lambda i, t, l: {"id": i, "type": t, "label": l}  # noqa: E731
    return {
        "architecture": {
            "nodes": [n("internet", "Internet", "Internet"), n("run1", "Cloud Run", "api"), n("run2", "Cloud Run", "worker"),
                      n("fn1", "Cloud Function", "etl"), n("sa1", "Service Account", "shared@p.iam"),
                      n("bq1", "BigQuery", "ds"), n("orph", "Bucket", "huerfano"), n("ext", "Endpoint externo", "api.x.com")],
            "edges": [{"source": "internet", "target": "run1", "kind": "public_access"},
                      {"source": "run1", "target": "sa1", "kind": "runs_as"}, {"source": "run2", "target": "sa1", "kind": "runs_as"},
                      {"source": "run1", "target": "bq1", "kind": "reads", "inferred": True},
                      {"source": "fn1", "target": "ext", "kind": "calls"}]},
        "workloads": [{"resource_id": "run1", "runtime_service_account": "shared@p.iam", "sa_is_default": True}],
        "service_identities": [{"resource_id": "shared@p.iam", "broad_roles": ["roles/editor"]}],
    }


def test_architecture_analysis_rules() -> None:
    out = analyze_architecture(arch_raw())
    rules = {r["rule"]: r for r in out["risks"]}
    assert set(rules) == {"ARCH_PUBLIC_ENTRYPOINT", "ARCH_SHARED_IDENTITY", "ARCH_PRIVILEGED_RUNTIME_IDENTITY",
                          "ARCH_DEFAULT_IDENTITY", "ARCH_INFERRED_DEPENDENCY", "ARCH_ISOLATED_COMPONENT",
                          "ARCH_EXTERNAL_ENDPOINT"}
    assert out["risks"][0]["severity"] == "HIGH"
    assert rules["ARCH_ISOLATED_COMPONENT"]["resources"] == ["orph", "run2"]
    assert out["summary"]["public_entrypoints"] == 1 and out["summary"]["components"] == 8
    assert all(f["mechanism"] not in ("runs_as", "has_role") for f in out["flows"])


def test_architecture_analysis_empty_is_safe() -> None:
    out = analyze_architecture({})
    assert out["risks"] == [] and out["summary"]["components"] == 0


def test_domain_kpis_only_use_domain_categories() -> None:
    f = lambda cat, rule: {"category": cat, "rule": rule, "resource_id": "x", "severity": "HIGH"}  # noqa: E731
    findings = [f("security", "IAM_ADMIN_ROLE"), f("pipelines", "FLOW_CYCLE")]
    infra = compute_domain_kpis({"iam_roles": [{}]}, findings, "infrastructure")
    assert set(infra["debt_scores"]) == {"security", "governance", "cost"} and infra["total_findings"] == 1
    data = compute_domain_kpis({}, findings, "data-governance")
    assert set(data["debt_scores"]) == {"data_governance", "pipelines", "capacity"} and data["total_findings"] == 1


def test_health_lists_domains() -> None:
    with TestClient(create_app(Settings())) as c:
        assert c.get("/health").json()["domains"] == DOMAIN_NODES


@pytest.mark.parametrize("domain", ["infrastructure", "architecture"])
def test_domain_job_flow(domain: str) -> None:
    with TestClient(create_app(Settings())) as c:
        r = c.post(f"/api/v1/{domain}/audits", json=BODY, headers=LLM)
        assert r.status_code == 202 and r.json()["domain"] == domain and r.json()["nodes"] == DOMAIN_NODES[domain]
        assert r.headers["location"] == f"/api/v1/{domain}/audits/{r.json()['job_id']}"
        j = wait(c, domain, r.json()["job_id"])
        assert j["status"] == "completed" and j["domain"] == domain
        res = j["result"]
        assert res["domain"] == domain and res["report"] and "excel_report" in res
        assert [e["node"] for e in j["events"]][0] == "discovery"
        if domain == "infrastructure":
            assert set(res["kpis"]["debt_scores"]) == {"security", "governance", "cost"}
            assert {f["category"] for f in res["findings"]} <= {"security", "governance", "cost"}
        else:
            assert res["architecture"]["summary"]["components"] >= 0 and res["raw_infra_data"] is None
        assert "sk-test-secret-123" not in str(j)


def test_data_governance_flow_and_include_raw() -> None:
    with TestClient(create_app(Settings())) as c:
        jid = c.post("/api/v1/data-governance/audits", json={**BODY, "history_days": 7}, headers=LLM).json()["job_id"]
        j = wait(c, "data-governance", jid)
        assert j["status"] == "completed" and j["result"]["report"]
        assert set(j["result"]["kpis"]["debt_scores"]) == {"data_governance", "pipelines", "capacity"}
        slim = c.get(f"/api/v1/data-governance/audits/{jid}?include_raw=false").json()
        assert slim["result"]["raw_infra_data"] is None


def test_domain_mismatch_is_404_and_legacy_unaffected() -> None:
    with TestClient(create_app(Settings())) as c:
        jid = c.post("/api/v1/infrastructure/audits", json=BODY, headers=LLM).json()["job_id"]
        wait(c, "infrastructure", jid)
        for other in ("architecture", "data-governance"):
            assert c.get(f"/api/v1/{other}/audits/{jid}").status_code == 404
            assert c.delete(f"/api/v1/{other}/audits/{jid}").status_code == 404
        assert c.get(f"/api/v1/audits/{jid}").status_code == 404           # heredado: solo trabajos "full"
        assert c.delete(f"/api/v1/infrastructure/audits/{jid}").status_code == 200


def test_domain_request_fields_are_scoped() -> None:
    with TestClient(create_app(Settings())) as c:
        r = c.post("/api/v1/architecture/audits", json={**BODY, "history_days": 5}, headers=LLM)
        assert r.status_code == 422                       # extra="forbid": el campo no pertenece al dominio
        r = c.post("/api/v1/infrastructure/audits", json={**BODY, "profile_datasets": ["a"]}, headers=LLM)
        assert r.status_code == 422
        assert c.post("/api/v1/infrastructure/audits", json=BODY).status_code == 401      # falta X-LLM-API-Key


def test_domain_sync_and_stream() -> None:
    with TestClient(create_app(Settings())) as c:
        r = c.post("/api/v1/infrastructure/audit", json=BODY, headers=LLM)
        assert r.status_code == 200 and r.json()["domain"] == "infrastructure"
        with c.stream("POST", "/api/v1/architecture/audit/stream", json=BODY, headers=LLM) as s:
            lines = [l for l in s.iter_lines() if l]
        assert '"start"' in lines[0] and '"architecture"' in lines[0] and '"completed"' in lines[-1]


def test_openapi_has_one_route_set_per_domain() -> None:
    with TestClient(create_app(Settings())) as c:
        paths = c.get("/openapi.json").json()["paths"]
        for d in DOMAINS:
            for suffix in ("/audits", "/audits/{job_id}", "/audit", "/audit/stream"):
                assert f"/api/v1/{d}{suffix}" in paths
        assert "/api/v1/data-governance/audits/multipart" in paths
        assert "/api/v1/architecture/audits/multipart" not in paths


def test_architecture_graph_runs_with_synthetic_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_discovery(state):
        return {"raw_infra_data": arch_raw(), "discovery_status": "complete"}
    monkeypatch.setattr(graph_mod, "discovery_node", fake_discovery)
    graph_mod.get_graph_for.cache_clear()
    with TestClient(create_app(Settings())) as c:
        jid = c.post("/api/v1/architecture/audits", json=BODY, headers=LLM).json()["job_id"]
        res = wait(c, "architecture", jid)["result"]
        assert res["architecture"]["summary"]["public_entrypoints"] == 1 and len(res["architecture"]["risks"]) == 7


def test_adc_used_when_enabled_and_no_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import types, sys
    from backend.agent_graph.nodes import discovery
    sentinel = object()
    fake = types.ModuleType("google.auth")
    fake.default = lambda scopes=None: (sentinel, "proj")
    monkeypatch.setitem(sys.modules, "google.auth", fake)
    import google
    monkeypatch.setattr(google, "auth", fake, raising=False)
    monkeypatch.delenv("ADC_ENABLED", raising=False)
    with pytest.raises(discovery.DiscoveryError):
        discovery.load_credentials(None)
    monkeypatch.setenv("ADC_ENABLED", "1")
    assert discovery.load_credentials(None) is sentinel


def test_gemini_uses_v1beta_for_system_messages() -> None:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from backend.core.llm_config import LLMFactory
    if "api_version" not in ChatGoogleGenerativeAI.model_fields:
        pytest.skip("versión antigua de langchain-google-genai")
    llm = LLMFactory._build_gemini("fake-key", "gemini-2.5-flash", 0.0)
    assert llm.api_version == "v1beta"
