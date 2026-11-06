"""KPIs por dominio: reutiliza el motor global y recalcula el índice general solo con las categorías del dominio."""
from __future__ import annotations

from typing import Any

from backend.core.kpi_engine import CATEGORY_WEIGHTS, _grade, compute_kpis

DOMAIN_CATEGORIES: dict[str, tuple[str, ...]] = {
    "infrastructure": ("security", "governance", "cost"),
    "data-governance": ("data_governance", "pipelines", "capacity"),
}


def compute_domain_kpis(raw: dict[str, Any], findings: list[dict[str, Any]], domain: str) -> dict[str, Any]:
    cats = DOMAIN_CATEGORIES[domain]
    full = compute_kpis(raw, [f for f in findings if f.get("category") in cats])
    dp = raw.get("data_platform") or {}
    has = {"security": True, "governance": True, "cost": True,
           "data_governance": bool(dp.get("tables") or dp.get("lake") or dp.get("flows")),
           "pipelines": bool(dp.get("flows")), "capacity": bool(dp.get("clusters"))}
    scores = {c: full["debt_scores"][c] for c in cats}
    active = [c for c in cats if has[c]]
    wsum = sum(CATEGORY_WEIGHTS[c] for c in active)
    overall = round(sum(scores[c] * CATEGORY_WEIGHTS[c] for c in active) / wsum, 1) if wsum else 0.0
    out: dict[str, Any] = {c: full[c] for c in cats}
    if domain == "infrastructure":
        out["cost"] = {k: v for k, v in full["cost"].items() if k != "data_platform_total_usd"}
    out.update({"inventory": full["inventory"], "debt_scores": scores, "overall_debt_score": overall,
                "grade": _grade(overall), "findings_by_severity": full["findings_by_severity"],
                "total_findings": full["total_findings"], "discarded_findings": full["discarded_findings"]})
    return out
