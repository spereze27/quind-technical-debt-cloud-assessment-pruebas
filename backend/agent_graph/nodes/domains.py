"""Nodos específicos de los dominios: KPIs por dominio, análisis de arquitectura e informes de infra/arquitectura."""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from backend.agent_graph.prompts import ARCHITECTURE_PROMPT, INFRA_REPORT_PROMPT
from backend.agent_graph.state import AuditState
from backend.core.architecture_analysis import analyze_architecture
from backend.core.domain_kpis import compute_domain_kpis
from backend.core.llm_config import get_llm_from_config

logger = logging.getLogger(__name__)
_SEV = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
MAX_CHARS = 120_000


def _kpi_node(domain: str):
    def node(state: AuditState) -> dict[str, Any]:
        try:
            return {"kpis": compute_domain_kpis(state.get("raw_infra_data") or {}, list(state.get("findings") or []), domain)}
        except Exception as exc:  # noqa: BLE001
            return {"kpis": {}, "errors": [f"kpi_calculator: {exc}"]}
    return node


infra_kpi_node = _kpi_node("infrastructure")
data_kpi_node = _kpi_node("data-governance")


def architecture_analyzer_node(state: AuditState) -> dict[str, Any]:
    try:
        return {"architecture": analyze_architecture(state.get("raw_infra_data") or {})}
    except Exception as exc:  # noqa: BLE001
        return {"architecture": {}, "errors": [f"architecture_analyzer: {type(exc).__name__}: {exc}"]}


def _text(msg: Any) -> str:
    return msg.content if isinstance(msg.content, str) else "".join(
        p.get("text", "") if isinstance(p, dict) else str(p) for p in msg.content)


def _llm_report(state: AuditState, config: RunnableConfig, prompt: str, payload: dict[str, Any], key: str,
                fallback: str) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))[:MAX_CHARS]
    try:
        llm = get_llm_from_config(config)
        msg = llm.invoke([SystemMessage(content=prompt.format(provider=state["provider"], target_id=state["target_id"])),
                          HumanMessage(content="Datos (JSON):\n```json\n" + body + "\n```")])
        return {key: _text(msg)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Informe de dominio falló")
        return {key: f"> ⚠️ El LLM no pudo redactar el informe ({type(exc).__name__}). Versión determinista:\n\n{fallback}",
                "errors": [f"report: {type(exc).__name__}: {exc}"]}


def infra_report_node(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    raw = state.get("raw_infra_data") or {}
    findings = sorted(state.get("findings") or [], key=lambda f: _SEV[f["severity"]])
    payload = {"context": {"provider": state["provider"], "scope": state["scope"], "target_id": state["target_id"],
                           "discovery_status": state.get("discovery_status", "unknown"),
                           "analysis_errors": state.get("errors", [])},
               "kpis": state.get("kpis", {}), "billing": raw.get("billing"),
               "findings": [{k: f[k] for k in ("severity", "category", "rule", "resource_id", "title", "recommendation",
                                                "estimated_monthly_waste_usd")} for f in findings]}
    k = state.get("kpis") or {}
    rows = "\n".join(f"| {f['severity']} | `{f['resource_id']}` | {f['title']} |" for f in findings)
    fallback = (f"**Deuda de infraestructura:** {k.get('overall_debt_score', 'N/A')} (grado {k.get('grade', 'N/A')})\n\n"
                f"| Severidad | Recurso | Hallazgo |\n|---|---|---|\n{rows}\n")
    return _llm_report(state, config, INFRA_REPORT_PROMPT, payload, "final_report", fallback)


def architecture_report_node(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    arch = state.get("architecture") or {}
    labels = {n["id"]: f'{n["type"]}: {n["label"]}' for n in arch.get("nodes", [])}
    payload = {"context": {"provider": state["provider"], "scope": state["scope"], "target_id": state["target_id"],
                           "discovery_status": state.get("discovery_status", "unknown"),
                           "analysis_errors": state.get("errors", [])},
               "summary": arch.get("summary"), "risks": arch.get("risks"), "flows": (arch.get("flows") or [])[:150],
               "components": [labels[i] for i in list(labels)[:200]], "asset_counts": arch.get("asset_counts")}
    rows = "\n".join(f"| {r['severity']} | {r['title']} | {r['count']} |" for r in arch.get("risks") or [])
    fallback = f"| Severidad | Riesgo | Recursos |\n|---|---|---|\n{rows}\n"
    return _llm_report(state, config, ARCHITECTURE_PROMPT, payload, "final_report", fallback)
