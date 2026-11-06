"""Construcción y compilación del StateGraph.

    START → discovery ─┬─► security_analyzer ────────┐
                       ├─► governance_analyzer ──────┤
                       ├─► cost_analyzer ────────────┼─► kpi_calculator ─┬─► report_generator ──────┐
                       ├─► data_governance_analyzer ─┤                   └─► data_report_generator ─┴─► excel_exporter → END
                       ├─► pipeline_analyzer ────────┤
                       └─► capacity_analyzer ────────┘
"""
from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.agent_graph.nodes import (
    capacity_node,
    cost_node,
    data_governance_node,
    data_report_node,
    pipelines_node,
    discovery_node,
    excel_export_node,
    governance_node,
    kpi_calculator_node,
    report_node,
    security_node,
)
from backend.agent_graph.nodes.domains import (
    architecture_analyzer_node,
    architecture_report_node,
    data_kpi_node,
    infra_kpi_node,
    infra_report_node,
)
from backend.agent_graph.state import AuditState

ANALYZERS = {
    "security_analyzer": security_node,
    "governance_analyzer": governance_node,
    "cost_analyzer": cost_node,
    "data_governance_analyzer": data_governance_node,
    "pipeline_analyzer": pipelines_node,
    "capacity_analyzer": capacity_node,
}

# Orden lógico para la UI de progreso.
NODE_SEQUENCE = ["discovery", *ANALYZERS, "kpi_calculator", "report_generator", "data_report_generator",
                 "excel_exporter"]


def build_graph() -> StateGraph:
    g = StateGraph(AuditState)
    g.add_node("discovery", discovery_node)
    for name, node in ANALYZERS.items():
        g.add_node(name, node)
    g.add_node("kpi_calculator", kpi_calculator_node)
    g.add_node("report_generator", report_node)
    g.add_node("data_report_generator", data_report_node)
    g.add_node("excel_exporter", excel_export_node)

    g.add_edge(START, "discovery")
    for name in ANALYZERS:                     # fan-out paralelo
        g.add_edge("discovery", name)
    g.add_edge(list(ANALYZERS), "kpi_calculator")   # fan-in: espera a todos los analizadores
    g.add_edge("kpi_calculator", "report_generator")        # informes en paralelo
    g.add_edge("kpi_calculator", "data_report_generator")
    g.add_edge(["report_generator", "data_report_generator"], "excel_exporter")   # espera a ambos informes
    g.add_edge("excel_exporter", END)
    return g


@lru_cache(maxsize=1)
def get_compiled_graph() -> CompiledStateGraph:
    return build_graph().compile()


# ---------------------------------------------------------------- dominios (un endpoint por dominio)
DOMAIN_ANALYZERS = {
    "infrastructure": ["security_analyzer", "governance_analyzer", "cost_analyzer"],
    "data-governance": ["data_governance_analyzer", "pipeline_analyzer", "capacity_analyzer"],
}
DOMAIN_NODES = {
    "infrastructure": ["discovery", *DOMAIN_ANALYZERS["infrastructure"], "kpi_calculator", "infra_report_generator"],
    "data-governance": ["discovery", *DOMAIN_ANALYZERS["data-governance"], "kpi_calculator",
                        "data_report_generator", "excel_exporter"],
    "architecture": ["discovery", "architecture_analyzer", "architecture_report_generator"],
    "full": NODE_SEQUENCE,
}
DOMAINS = tuple(d for d in DOMAIN_NODES if d != "full")


def build_domain_graph(domain: str) -> StateGraph:
    g = StateGraph(AuditState)
    g.add_node("discovery", discovery_node)
    g.add_edge(START, "discovery")
    if domain == "architecture":
        g.add_node("architecture_analyzer", architecture_analyzer_node)
        g.add_node("architecture_report_generator", architecture_report_node)
        g.add_edge("discovery", "architecture_analyzer")
        g.add_edge("architecture_analyzer", "architecture_report_generator")
        g.add_edge("architecture_report_generator", END)
        return g
    names = DOMAIN_ANALYZERS[domain]
    for n in names:
        g.add_node(n, ANALYZERS[n])
        g.add_edge("discovery", n)
    if domain == "infrastructure":
        g.add_node("kpi_calculator", infra_kpi_node)
        g.add_node("infra_report_generator", infra_report_node)
        g.add_edge(names, "kpi_calculator")
        g.add_edge("kpi_calculator", "infra_report_generator")
        g.add_edge("infra_report_generator", END)
    else:
        g.add_node("kpi_calculator", data_kpi_node)
        g.add_node("data_report_generator", data_report_node)
        g.add_node("excel_exporter", excel_export_node)
        g.add_edge(names, "kpi_calculator")
        g.add_edge("kpi_calculator", "data_report_generator")
        g.add_edge("data_report_generator", "excel_exporter")
        g.add_edge("excel_exporter", END)
    return g


@lru_cache(maxsize=8)
def get_graph_for(domain: str) -> CompiledStateGraph:
    return get_compiled_graph() if domain == "full" else build_domain_graph(domain).compile()
