#!/usr/bin/env python3
"""Prueba de humo de la API (solo librería estándar). Verifica salud, autenticación, validación y, opcionalmente,
ejecuta un análisis real por dominio.

  python api_tests/smoke_test.py --url https://SERVICIO.run.app --api-key KEY                     # sin gastar LLM
  python api_tests/smoke_test.py --url ... --api-key KEY --llm-key sk-... --llm-provider anthropic \\
         --project mi-proyecto --run infrastructure architecture data-governance                  # análisis reales
Variables equivalentes: BASE_URL, API_KEY, LLM_API_KEY, LLM_PROVIDER, GCP_PROJECT.
Código de salida 0 = todo bien.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DOMAINS = ("infrastructure", "data-governance", "architecture")
FAILS: list[str] = []


def call(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 60) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if "json" in r.headers.get("content-type", "") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FALLA'} {name} {extra}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("BASE_URL", "http://localhost:8000"))
    ap.add_argument("--api-key", default=os.getenv("API_KEY", ""))
    ap.add_argument("--llm-key", default=os.getenv("LLM_API_KEY", ""))
    ap.add_argument("--llm-provider", default=os.getenv("LLM_PROVIDER", "anthropic"))
    ap.add_argument("--project", default=os.getenv("GCP_PROJECT", ""))
    ap.add_argument("--run", nargs="*", default=[], choices=DOMAINS, help="dominios a ejecutar de verdad")
    ap.add_argument("--timeout", type=int, default=1800, help="segundos máximos por análisis")
    ap.add_argument("--auth-expected", choices=["on", "off"], default="on")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    auth = {"X-API-Key": a.api_key} if a.api_key else {}

    print("1) Salud")
    st, h = call("GET", f"{base}/health", {})
    check("GET /health = 200", st == 200 and isinstance(h, dict) and h.get("status") == "ok", f"(versión {h.get('version') if isinstance(h, dict) else '?'})")
    if isinstance(h, dict):
        check("auth activa", h.get("auth_enabled") is (a.auth_expected == "on"), f"(auth_enabled={h.get('auth_enabled')})")
        check("expone los 3 dominios", all(d in h.get("domains", {}) for d in DOMAINS))
        print(f"       modo de descubrimiento: {h.get('discovery_mode')}; faltan módulos GCP: {h.get('missing_gcp_modules')}")

    print("2) Documentación")
    st, spec = call("GET", f"{base}/openapi.json", {})
    check("GET /openapi.json", st == 200 and isinstance(spec, dict))
    if isinstance(spec, dict):
        for d in DOMAINS:
            check(f"rutas de {d}", f"/api/v1/{d}/audits" in spec["paths"])

    print("3) Seguridad y validación (no consumen LLM)")
    for d in DOMAINS:
        body = {"provider": "GCP", "scope": "project", "target_id": a.project or "proyecto-prueba"}
        if a.auth_expected == "on":
            st, _ = call("POST", f"{base}/api/v1/{d}/audits", {}, body)
            check(f"{d}: sin X-API-Key → 401", st == 401, f"(recibido {st})")
        st, r = call("POST", f"{base}/api/v1/{d}/audits", auth, body)
        check(f"{d}: sin X-LLM-API-Key → 401", st == 401 and isinstance(r, dict) and r.get("code") == "missing_llm_key", f"(recibido {st})")
        st, r = call("POST", f"{base}/api/v1/{d}/audits", {**auth, "X-LLM-API-Key": "x"}, {**body, "campo_inexistente": 1})
        check(f"{d}: campo desconocido → 422", st == 422, f"(recibido {st})")
        st, _ = call("GET", f"{base}/api/v1/{d}/audits/{'0' * 32}", auth)
        check(f"{d}: job inexistente → 404", st == 404, f"(recibido {st})")

    for d in a.run:
        print(f"4) Análisis real: {d}")
        if not (a.llm_key and a.project):
            check(f"{d}: requiere --llm-key y --project", False)
            continue
        body = {"provider": "GCP", "scope": "project", "target_id": a.project, "llm_provider": a.llm_provider}
        st, job = call("POST", f"{base}/api/v1/{d}/audits", {**auth, "X-LLM-API-Key": a.llm_key}, body)
        check(f"{d}: crear → 202", st == 202 and isinstance(job, dict), f"(recibido {st}: {job if st != 202 else ''})")
        if st != 202:
            continue
        t0, last = time.time(), None
        while time.time() - t0 < a.timeout:
            st, j = call("GET", f"{base}/api/v1/{d}/audits/{job['job_id']}?include_raw=false", auth)
            if j.get("status") != last:
                last = j.get("status")
                print(f"       estado: {last} ({int(time.time() - t0)} s, nodos: {[e['node'] for e in j.get('events', [])]})")
            if last in ("completed", "error", "cancelled"):
                break
            time.sleep(3)
        check(f"{d}: terminó 'completed'", last == "completed", f"(estado {last}; error={j.get('error')})")
        res = (j or {}).get("result") or {}
        check(f"{d}: trae informe", bool(res.get("report")))
        print(f"       descubrimiento={res.get('discovery_status')} hallazgos={len(res.get('findings', []))} "
              f"deuda={res.get('kpis', {}).get('overall_debt_score')} avisos={len(res.get('errors', []))}")
        for e in res.get("errors", [])[:5]:
            print(f"       aviso: {e[:160]}")

    print("\nRESULTADO:", "TODO OK" if not FAILS else f"{len(FAILS)} fallas -> {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
