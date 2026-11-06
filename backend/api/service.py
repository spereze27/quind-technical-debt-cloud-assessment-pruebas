"""Lógica de negocio de la API: preparar el estado del grafo, credenciales temporales y ejecución con eventos."""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator

from backend.agent_graph.graph import get_graph_for
from backend.api.errors import ApiError, scrub
from backend.api.schemas import BaseRequest
from backend.api.settings import Settings
from backend.core.llm_config import LLMConfigError, LLMFactory, LLMSettings

logger = logging.getLogger("api")


# ------------------------------------------------------------------ entradas auxiliares
@contextmanager
def credentials_file(raw: str | None) -> Iterator[str | None]:
    """Escribe el JSON de la Service Account en un temporal 0600 y lo borra SIEMPRE al salir."""
    if not raw:
        yield None
        return
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "Las credenciales cloud no son un JSON válido.", "invalid_credentials") from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ApiError(400, "El JSON debe ser una clave de Service Account (type=service_account).",
                       "invalid_credentials")
    fd, path = tempfile.mkstemp(prefix="sa_", suffix=".json")
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(info, fh)
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            logger.warning("No se pudo eliminar el archivo temporal de credenciales")


def cleanup_stale_credentials(max_age_s: int = 3600) -> int:
    """Borra archivos `sa_*.json` huérfanos (p. ej. tras un SIGKILL/OOM en mitad de una auditoría)."""
    import glob
    import time

    removed = 0
    for path in glob.glob(os.path.join(tempfile.gettempdir(), "sa_*.json")):
        try:
            if time.time() - os.path.getmtime(path) > max_age_s:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def parse_catalog(text: str | None) -> list[dict[str, Any]]:
    """Catálogo opcional de flujos: YAML o JSON; una lista o un objeto con clave `flows`."""
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml

            data = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(400, f"El catálogo de flujos no es YAML/JSON válido: {type(exc).__name__}",
                           "invalid_catalog") from exc
    flows = data.get("flows") if isinstance(data, dict) else data
    if not isinstance(flows, list) or not all(isinstance(f, dict) for f in flows):
        raise ApiError(400, "El catálogo debe ser una lista de flujos (o {flows: [...]}).", "invalid_catalog")
    return flows[:500]


def decode_dictionary(req: Any, settings: Settings) -> dict[str, Any] | None:
    """Decodifica y valida el diccionario. Requiere el módulo `backend.core.dictionary` (v1.5)."""
    if not req.data_dictionary_b64:
        return None
    try:
        content = base64.b64decode(req.data_dictionary_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, "data_dictionary_b64 no es Base64 válido.", "invalid_dictionary") from exc
    if len(content) > settings.max_dictionary_bytes:
        raise ApiError(400, f"El diccionario supera {settings.max_dictionary_mb} MB.", "invalid_dictionary")
    try:
        from backend.core.dictionary import DictionaryError, parse_dictionary
    except ImportError as exc:
        raise ApiError(501, "Este despliegue no incluye el módulo de diccionario de datos (backend/core/dictionary.py).",
                       "not_implemented") from exc
    try:
        return parse_dictionary(content, req.data_dictionary_name or "diccionario.xlsx")
    except DictionaryError as exc:
        raise ApiError(400, str(exc), "invalid_dictionary") from exc


# ------------------------------------------------------------------ preparación
def prepare(req: BaseRequest, llm_api_key: str | None, settings: Settings, domain: str = "full") -> tuple[dict[str, Any], dict[str, Any]]:
    """Valida todo ANTES de lanzar el grafo y devuelve (estado inicial, config). Sin efectos secundarios."""
    if not llm_api_key:
        raise ApiError(401, "Falta la cabecera X-LLM-API-Key.", "missing_llm_key")
    try:
        LLMFactory.create(LLMSettings(provider=req.llm_provider, api_key=llm_api_key, model=req.llm_model))
    except LLMConfigError as exc:
        raise ApiError(400, scrub(str(exc), [llm_api_key]), "invalid_llm_config") from exc
    except ImportError as exc:
        raise ApiError(500, f"Dependencia no instalada: {exc.name}", "missing_dependency") from exc
    with credentials_file(req.cloud_credentials):      # valida el JSON sin conservar el archivo
        pass

    g = lambda name, default=None: getattr(req, name, default)  # noqa: E731 — los dominios no tienen todos los campos
    state: dict[str, Any] = {
        "provider": req.provider, "scope": req.scope, "target_id": req.target_id, "domain": domain,
        "has_credentials": bool(req.cloud_credentials), "findings": [], "errors": [],
        "billing_export_table": (g("billing_export_table") or "").strip() or None,
        # infraestructura y arquitectura no necesitan la plataforma de datos: se omite su recolección
        "data_options": {"enabled": domain in ("full", "data-governance"),
                         "history_days": g("history_days", 30), "sample_lake": g("sample_lake", True),
                         "read_dags": g("read_dags", True), "max_projects": g("max_projects", 10),
                         "catalog": parse_catalog(g("flow_catalog"))},
    }
    dictionary = decode_dictionary(req, settings) if g("data_dictionary_b64") else None
    if dictionary is not None:
        state["data_dictionary"] = dictionary
        state["data_options"]["profile"] = {
            "project": g("profile_project") or req.target_id, "datasets": g("profile_datasets", []),
            "max_gb": g("profile_max_gb", 5.0)}
    config = {"configurable": {"llm_provider": req.llm_provider, "llm_api_key": llm_api_key, "llm_model": req.llm_model},
              "run_name": f"audit-{req.provider}-{req.target_id}", "max_concurrency": 6}
    return state, config


_PUBLIC_KEYS = ("provider", "scope", "target_id", "raw_infra_data", "findings", "kpis", "final_report", "data_report",
                "errors", "discovery_status", "data_profile", "dictionary_report")
_DOMAIN_REPORT_KEY = {"infrastructure": "final_report", "data-governance": "data_report", "architecture": "final_report"}


def public_result(state: dict[str, Any]) -> dict[str, Any]:
    """Resultado expuesto: nunca incluye rutas de archivos ni credenciales."""
    domain = state.get("domain", "full")
    if domain != "full":
        out = {"domain": domain, **{k: state.get(k) for k in ("provider", "scope", "target_id", "discovery_status", "findings",
                                                               "kpis", "errors", "raw_infra_data")} | {"kpis": state.get("kpis") or {}, "findings": state.get("findings") or [], "errors": state.get("errors") or []},
               "report": state.get(_DOMAIN_REPORT_KEY[domain])}
        if domain == "architecture":
            out["architecture"] = state.get("architecture") or {}
            out["raw_infra_data"] = None                 # el grafo ya viaja en `architecture`
        rep = state.get("excel_report")
        out["excel_report"] = {k: v for k, v in rep.items() if k != "path"} if rep else None
        return out
    out = {k: state.get(k) for k in _PUBLIC_KEYS}
    for key in ("excel_report", "profile_excel"):
        rep = state.get(key)
        out[key] = {k: v for k, v in rep.items() if k != "path"} if rep else None
    return out


# ------------------------------------------------------------------ ejecución
async def graph_events(req: BaseRequest, state: dict[str, Any], config: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Emite `node_completed` por nodo y al final `completed` (con el resultado) o `error` (mensaje saneado)."""
    secrets = [config["configurable"].get("llm_api_key")]
    final: dict[str, Any] = dict(state)
    try:
        with credentials_file(req.cloud_credentials) as cred_path:
            state["credentials_path"] = cred_path
            async for chunk in get_graph_for(state.get("domain", "full")).astream(state, config=config, stream_mode="updates"):
                for node, update in chunk.items():
                    update = update or {}
                    for key, val in update.items():           # replica los reducers para el estado final
                        final[key] = final.get(key, []) + val if key in ("findings", "errors") else val
                    yield {"event": "node_completed", "node": node,
                           "summary": {"findings": len(update.get("findings", [])),
                                       "errors": [scrub(str(e), secrets) for e in update.get("errors", [])]}}
        result = public_result(final)
        result["errors"] = [scrub(str(e), secrets) for e in result.get("errors") or []]
        yield {"event": "completed", "result": result}
    except ApiError as exc:
        yield {"event": "error", "detail": exc.detail}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Auditoría falló")
        yield {"event": "error", "detail": scrub(f"{type(exc).__name__}: {exc}", secrets)[:500]}
