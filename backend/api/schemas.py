"""Modelos de la API (entrada y salida). Son el contrato: `/openapi.json` se genera de aquí."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
JobStatusName = Literal["queued", "running", "completed", "error", "cancelled"]
DiscoveryStatus = Literal["complete", "partial", "failed", "mock"]

_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
_DATASET_RE = re.compile(r"^[A-Za-z0-9_]+$")


# ------------------------------------------------------------------ entrada
class BaseRequest(BaseModel):
    """Campos comunes a todos los dominios."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["AWS", "GCP", "Azure"] = Field(description="Nube a auditar. Solo GCP tiene descubrimiento real.")
    scope: Literal["organization", "folder", "project"] = Field(description="Alcance de la auditoría.")
    target_id: str = Field(min_length=3, max_length=128, description="ID del proyecto, carpeta u organización.")
    llm_provider: Literal["openai", "anthropic", "gemini"] = "openai"
    llm_model: str | None = Field(default=None, max_length=100)
    cloud_credentials: str | None = Field(default=None, max_length=20_000,
                                          description="JSON de una Service Account, como texto.")

    @field_validator("target_id")
    @classmethod
    def _target(cls, v: str) -> str:
        v = v.strip()
        if not _ID_RE.match(v):
            raise ValueError("target_id solo admite letras, números, '-', '_' y '.'")
        return v


class ArchitectureRequest(BaseRequest):
    """`POST /api/v1/architecture/audits`: solo el grafo de recursos, identidades y flujos."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "provider": "GCP", "scope": "project", "target_id": "mi-proyecto-poc", "llm_provider": "anthropic"}]})


class InfrastructureRequest(BaseRequest):
    """`POST /api/v1/infrastructure/audits`: seguridad, gobierno y costos."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "provider": "GCP", "scope": "project", "target_id": "mi-proyecto-poc", "llm_provider": "anthropic"}]})
    billing_export_table: str | None = Field(default=None, max_length=300,
                                             description="proyecto.dataset.tabla del export de facturación.")


class DataGovernanceRequest(InfrastructureRequest):
    """`POST /api/v1/data-governance/audits`: BigQuery, data lake, flujos y capacidad."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "provider": "GCP", "scope": "project", "target_id": "mi-proyecto-poc", "llm_provider": "anthropic",
        "history_days": 30, "profile_datasets": ["credito", "ml"]}]})
    history_days: int = Field(default=30, ge=1, le=180)
    sample_lake: bool = True
    read_dags: bool = True
    max_projects: int = Field(default=10, ge=1, le=50)
    flow_catalog: str | None = Field(default=None, max_length=500_000, description="YAML/JSON de flujos externos.")
    # --- diccionario de datos (v1.5)
    data_dictionary_b64: str | None = Field(default=None, max_length=14_500_000,
                                            description="Archivo .xlsx/.csv del diccionario, en Base64 (máx. 10 MB).")
    data_dictionary_name: str | None = Field(default=None, max_length=255, description="Nombre con extensión.")
    profile_project: str | None = Field(default=None, max_length=128, description="Proyecto BigQuery a perfilar.")
    profile_datasets: list[str] = Field(default_factory=list, max_length=50, description="Datasets a perfilar.")
    profile_max_gb: float = Field(default=5.0, gt=0, le=500, description="GB máximos a leer en BigQuery.")

    @field_validator("profile_project")
    @classmethod
    def _pproject(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v and not _PROJECT_RE.match(v):
            raise ValueError("profile_project tiene caracteres no permitidos")
        return v

    @field_validator("profile_datasets", mode="before")
    @classmethod
    def _datasets(cls, v: Any) -> Any:
        if isinstance(v, str):                       # acepta "credito,ml"
            v = [x.strip() for x in v.split(",")]
        if isinstance(v, list):
            v = [str(x).strip() for x in v if str(x).strip()]
            if any(not _DATASET_RE.match(x) or len(x) > 128 for x in v):
                raise ValueError("profile_datasets solo admite letras, números y '_'")
        return v


class AuditRequest(DataGovernanceRequest):
    """Auditoría completa (endpoint heredado `POST /api/v1/audits`): los tres dominios en un solo trabajo."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "provider": "GCP", "scope": "project", "target_id": "mi-proyecto-poc", "llm_provider": "anthropic",
        "history_days": 30, "profile_datasets": ["credito", "ml"]}]})


# ------------------------------------------------------------------ salida
class Finding(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str | None = None
    category: str = Field(description="security | governance | cost | data_governance | pipelines | capacity | data_quality")
    rule: str
    resource_id: str
    resource_type: str = ""
    severity: Severity
    title: str = ""
    description: str = ""
    recommendation: str = ""
    estimated_monthly_waste_usd: float = 0.0


class ExcelReport(BaseModel):
    model_config = ConfigDict(extra="allow")
    report_id: str = Field(description="Descargable en GET /api/v1/reports/{report_id}.xlsx durante 24 h.")
    filename: str
    sheets: dict[str, int] = Field(default_factory=dict, description="Hoja → número de filas.")


class AuditResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str | None = None
    scope: str | None = None
    target_id: str | None = None
    discovery_status: DiscoveryStatus | None = None
    errors: list[str] = Field(default_factory=list, description="Avisos y fallos parciales; nunca son fatales.")
    findings: list[Finding] = Field(default_factory=list)
    kpis: dict[str, Any] = Field(default_factory=dict)
    final_report: str | None = None
    data_report: str | None = None
    excel_report: ExcelReport | None = None
    data_profile: dict[str, Any] | None = Field(default=None, description="Primer vistazo (null sin diccionario).")
    dictionary_report: str | None = None
    profile_excel: ExcelReport | None = None
    raw_infra_data: dict[str, Any] | None = Field(default=None, description="Inventario crudo; estructura no estable.")


class DomainResult(BaseModel):
    """Resultado de un dominio. `report` es el informe en Markdown; el resto depende del dominio."""
    model_config = ConfigDict(extra="allow")
    domain: str
    provider: str | None = None
    scope: str | None = None
    target_id: str | None = None
    discovery_status: DiscoveryStatus | None = None
    errors: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    kpis: dict[str, Any] = Field(default_factory=dict)
    report: str | None = None
    architecture: dict[str, Any] | None = Field(default=None, description="Solo dominio architecture.")
    excel_report: ExcelReport | None = None
    raw_infra_data: dict[str, Any] | None = None


class JobEvent(BaseModel):
    event: Literal["node_completed"] = "node_completed"
    node: str
    summary: dict[str, Any] = Field(default_factory=dict)


class JobCreated(BaseModel):
    job_id: str
    status: JobStatusName
    domain: str = "full"
    nodes: list[str]


class JobStatus(BaseModel):
    job_id: str
    status: JobStatusName
    elapsed_s: float
    events: list[JobEvent] = Field(default_factory=list)
    error: str | None = None
    result: AuditResult | None = None


class DomainJobStatus(BaseModel):
    job_id: str
    domain: str
    status: JobStatusName
    elapsed_s: float
    events: list[JobEvent] = Field(default_factory=list)
    error: str | None = None
    result: DomainResult | None = None


class JobCancelled(BaseModel):
    job_id: str
    status: JobStatusName


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    nodes: list[str]
    domains: dict[str, list[str]] = Field(default_factory=dict, description="Dominio → nodos del grafo.")
    discovery_mode: str
    missing_gcp_modules: list[str]
    auth_enabled: bool
    job_store: str
    active_jobs: int
    max_concurrent: int


class ErrorResponse(BaseModel):
    detail: str | list[dict[str, Any]]
    code: str
    request_id: str | None = None


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Entrada inválida (credenciales, catálogo, diccionario o clave del LLM)."},
    401: {"model": ErrorResponse, "description": "Falta X-API-Key o X-LLM-API-Key, o son inválidas."},
    404: {"model": ErrorResponse, "description": "Recurso inexistente o vencido."},
    413: {"model": ErrorResponse, "description": "Cuerpo demasiado grande."},
    422: {"model": ErrorResponse, "description": "Campos con tipo o valor inválido (sin eco del valor enviado)."},
    429: {"model": ErrorResponse, "description": "Límite de solicitudes o cola llena."},
    500: {"model": ErrorResponse, "description": "Error interno."},
}
