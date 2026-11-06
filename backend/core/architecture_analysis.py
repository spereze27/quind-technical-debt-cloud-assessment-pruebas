"""Análisis DETERMINISTA de la arquitectura descubierta (grafo de recursos, identidades y flujos).

Entrada: `raw_infra_data` (claves `architecture{nodes,edges}`, `workloads`, `service_identities`, `databases`, `asset_counts`).
Salida: resumen, flujos legibles y riesgos de arquitectura con reglas cerradas (prefijo ARCH_).
No usa LLM: las cifras son reproducibles; el LLM solo redacta la narrativa sobre este resultado.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

IDENTITY_TYPES = {"Service Account"}
STRUCTURAL_EDGES = {"has_role", "runs_as"}
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

RULES: dict[str, tuple[str, str, str]] = {
    # regla: (severidad, título, recomendación)
    "ARCH_PUBLIC_ENTRYPOINT": ("HIGH", "Recursos expuestos a internet",
                               "Restringir el acceso (IAM, ingress interno, redes autorizadas) o poner un balanceador con autenticación."),
    "ARCH_SHARED_IDENTITY": ("MEDIUM", "Una misma cuenta de servicio ejecuta varios componentes",
                             "Crear una cuenta de servicio por componente con los permisos mínimos necesarios."),
    "ARCH_PRIVILEGED_RUNTIME_IDENTITY": ("HIGH", "Componente que corre con una identidad de permisos amplios",
                                         "Reemplazar roles amplios (owner, editor, admin) por roles predefinidos o personalizados mínimos."),
    "ARCH_DEFAULT_IDENTITY": ("MEDIUM", "Componente que usa la cuenta de servicio por defecto",
                              "Asignar una cuenta de servicio dedicada; la cuenta por defecto suele tener el rol Editor."),
    "ARCH_INFERRED_DEPENDENCY": ("LOW", "Dependencias inferidas que conviene confirmar",
                                 "Validar con el equipo dueño; documentar la relación o eliminar la referencia en las variables de entorno."),
    "ARCH_ISOLATED_COMPONENT": ("LOW", "Componentes sin relaciones con otros recursos",
                                "Confirmar si siguen en uso; si no, retirarlos para reducir costo y superficie de ataque."),
    "ARCH_EXTERNAL_ENDPOINT": ("LOW", "Componentes que invocan endpoints externos al proyecto",
                               "Documentar el destino, el método de autenticación y el responsable de la integración."),
}


def _label(nodes: dict[str, dict[str, Any]], nid: str) -> str:
    n = nodes.get(nid)
    return f"{n['type']}: {n['label']}" if n else str(nid)


def _risk(rule: str, description: str, resources: list[str], severity: str | None = None) -> dict[str, Any]:
    sev, title, rec = RULES[rule]
    return {"rule": rule, "severity": severity or sev, "title": title, "description": description,
            "resources": sorted(set(resources))[:50], "count": len(set(resources)), "recommendation": rec}


def analyze_architecture(raw: dict[str, Any]) -> dict[str, Any]:
    arch = raw.get("architecture") or {}
    node_list, edges = arch.get("nodes") or [], arch.get("edges") or []
    nodes = {n["id"]: n for n in node_list}
    workloads = raw.get("workloads") or []
    identities = {i["resource_id"]: i for i in raw.get("service_identities") or []}

    flows = [{"source": e["source"], "from": _label(nodes, e["source"]), "mechanism": e["kind"],
              "target": e["target"], "to": _label(nodes, e["target"]), "identity": e.get("via"),
              "permissions": e.get("permissions") or [], "detail": e.get("detail"), "inferred": bool(e.get("inferred"))}
             for e in edges if e["kind"] not in STRUCTURAL_EDGES]

    risks: list[dict[str, Any]] = []

    # 1) Exposición pública
    public = [e for e in edges if e["kind"] == "public_access"]
    if public:
        targets = [e["target"] for e in public]
        risks.append(_risk("ARCH_PUBLIC_ENTRYPOINT",
                           f"{len(set(targets))} recursos aceptan tráfico o permisos de internet: "
                           + "; ".join(sorted({_label(nodes, t) for t in targets})[:8]), targets))

    # 2) Identidades compartidas por varios componentes
    users: dict[str, set[str]] = {}
    for e in edges:
        if e["kind"] == "runs_as":
            users.setdefault(e["target"], set()).add(e["source"])
    shared = {sa: u for sa, u in users.items() if len(u) >= 2}
    if shared:
        detail = "; ".join(f"{nodes.get(sa, {}).get('label', sa)} → {len(u)} componentes" for sa, u in
                           sorted(shared.items(), key=lambda kv: -len(kv[1]))[:6])
        risks.append(_risk("ARCH_SHARED_IDENTITY", f"{len(shared)} cuentas de servicio compartidas: {detail}.",
                           [x for u in shared.values() for x in u]))

    # 3) Identidad de ejecución con permisos amplios / cuenta por defecto
    priv, default = [], []
    for w in workloads:
        ident = identities.get(w.get("runtime_service_account") or "") or {}
        if ident.get("broad_roles") or ident.get("privilege_escalation_roles") or ident.get("admin_roles"):
            priv.append(w["resource_id"])
        if w.get("sa_is_default") or ident.get("is_default_sa"):
            default.append(w["resource_id"])
    if priv:
        risks.append(_risk("ARCH_PRIVILEGED_RUNTIME_IDENTITY",
                           f"{len(priv)} componentes corren con una identidad con roles amplios, de administración o de escalamiento.",
                           priv))
    if default:
        risks.append(_risk("ARCH_DEFAULT_IDENTITY",
                           f"{len(default)} componentes usan la cuenta de servicio por defecto.", default))

    # 4) Relaciones inferidas
    inferred = [e for e in edges if e.get("inferred")]
    if inferred:
        risks.append(_risk("ARCH_INFERRED_DEPENDENCY",
                           f"{len(inferred)} relaciones se infirieron por coincidencia de nombres en variables de entorno.",
                           [e["source"] for e in inferred]))

    # 5) Componentes aislados (sin relaciones funcionales)
    linked = {x for e in edges if e["kind"] not in STRUCTURAL_EDGES for x in (e["source"], e["target"])}
    isolated = [nid for nid, n in nodes.items()
                if n["type"] not in IDENTITY_TYPES and n["type"] not in ("Internet", "Endpoint externo")
                and nid not in linked and nid != "internet"]
    if isolated:
        risks.append(_risk("ARCH_ISOLATED_COMPONENT", f"{len(isolated)} componentes no tienen ninguna relación funcional.",
                           isolated))

    # 6) Endpoints externos
    ext = [e for e in edges if nodes.get(e["target"], {}).get("type") == "Endpoint externo"]
    if ext:
        risks.append(_risk("ARCH_EXTERNAL_ENDPOINT", f"{len(ext)} relaciones salen del proyecto hacia endpoints externos.",
                           [e["source"] for e in ext]))

    risks.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]], -r["count"]))
    by_type = Counter(n["type"] for n in node_list)
    summary = {
        "components": len(node_list), "relationships": len(flows),
        "components_by_type": dict(by_type.most_common()),
        "relationships_by_mechanism": dict(Counter(f["mechanism"] for f in flows).most_common()),
        "workloads": len(workloads), "service_accounts": by_type.get("Service Account", 0),
        "public_entrypoints": len({e["target"] for e in public}), "inferred_relationships": len(inferred),
        "risks_by_severity": dict(Counter(r["severity"] for r in risks)),
    }
    return {"summary": summary, "nodes": node_list, "edges": edges, "flows": flows[:500], "risks": risks,
            "asset_counts": raw.get("asset_counts"), "enabled_apis": (raw.get("enabled_apis") or [])[:200]}
