# ☁️ Cloud Tech Debt Analyzer

Auditoría multi-agente (**LangGraph**) de la **deuda técnica** de una arquitectura en Google Cloud: seguridad,
gobierno, costos, **gobierno de datos**, **flujos/pipelines** y **dimensionamiento de clusters**.
Solo API REST (**FastAPI**), sin interfaz: cualquier cliente consume los endpoints.

## Qué analiza

| Área | Qué obtiene | De dónde |
|---|---|---|
| Arquitectura de servicios | Qué recurso habla con cuál, por qué mecanismo (HTTP/OIDC, push Pub/Sub, conector SQL, secreto, rol IAM), con qué cuenta de servicio y qué permisos tiene | Cloud Asset Inventory |
| **Flujos** (pipelines y microservicios) | Entradas (Postgres, HDFS, GCS, Pub/Sub, BigQuery…), salidas, capa, formato, **modo de escritura** (append / sobrescribe / merge), calendario, estado, duración media/p95, tendencia, costo | Historial de jobs de BigQuery, consultas programadas, Dataflow, Dataproc, DAGs de Composer, Datastream, Cloud Run/Functions, catálogo del usuario |
| **Contradicciones e inactividad** | Varios flujos escribiendo el mismo destino con modos incompatibles, ciclos, lógica duplicada, flujos inactivos o con fallos, salidas que nadie consume | Linaje construido a partir de lo anterior |
| **Rendimiento** | Flujos lentos o que se degradan, escaneos completos pese a particiones, recálculos totales, `SELECT *` | Bytes/slots/duración por ejecución vs tamaño de entradas |
| **Gobierno de datos** | Capa medallion de cada tabla/bucket y si el dato está en la capa correcta, saltos/inversiones de capa, bronze sobrescrito, normalización vs lo esperado por capa, tipos incorrectos, PII, particionado, modelo de facturación de almacenamiento | `INFORMATION_SCHEMA` de BigQuery + linaje |
| **Data lake** | Formatos (Parquet/Avro vs CSV/JSON), archivos pequeños, clase de almacenamiento vs antigüedad y accesos, ciclo de vida, particionado de rutas, capas mezcladas | Muestreo de objetos de GCS + Cloud Monitoring |
| **Capacidad** | Clusters Dataproc/GKE, Cloud SQL y Composer: sobredimensionado / subdimensionado / adecuado, **por qué** (uso p95 de CPU y memoria, horas activas) y **costo actual vs recomendado** | Configuración + Cloud Monitoring (14 días) + precios de lista |
| **Costos por proyecto → flujo** | Costo estimado a 30 días de cada flujo + costos compartidos (almacenamiento, capacidad ociosa) | BigQuery bytes/slots, Dataflow vCPU/memoria, prorrateo de clusters, tiempo facturable de Cloud Run; costo real si hay export de facturación |

Con alcance **Carpeta** u **Organización** recorre todos los proyectos (y subcarpetas) y agrupa
**carpeta → proyecto → flujo**.

## Grafo de agentes

```
START → discovery ─┬─► security_analyzer ────────┐
                   ├─► governance_analyzer ──────┤
                   ├─► cost_analyzer ────────────┼─► kpi_calculator ─┬─► report_generator ──────┐
                   ├─► data_governance_analyzer ─┤                   └─► data_report_generator ─┴─► END
                   ├─► pipeline_analyzer ────────┤
                   └─► capacity_analyzer ────────┘
```

- **Deterministas (Python)**: descubrimiento, motor de plataforma de datos (`core/data_platform.py`: capas,
  normalización, linaje, conflictos, costos), dimensionamiento (`core/rightsizing.py`), precios
  (`core/pricing.py`), KPIs y el anexo técnico del informe de datos. **El LLM nunca calcula cifras.**
- **LLM**: seis analizadores especialistas y dos redactores. Cada señal determinista se convierte también en
  hallazgo aunque el LLM la omita (`core/data_findings.py`), así ningún problema detectado se pierde.

## Interfaz

Pestañas: **KPIs** (6 índices de deuda) · **Arquitectura** (esquema por capas con íconos oficiales de GCP) ·
**Flujos** (treemap de costos carpeta→proyecto→flujo, linaje bronze→silver→gold con íconos, detalle de cada flujo
con entradas/salidas/modo/duraciones) · **Datos** (medallion, tablas, lake) · **Capacidad** · **Identidades** ·
**Facturación** · **Informe** · **Informe de datos** (con diagramas Mermaid y anexo técnico) · **Hallazgos** · **Inventario**.

### Catálogo de flujos externos (opcional)
Para fuentes que GCP no ve (HDFS on-premise, Postgres externos, SFTP…), sube un YAML/JSON en el panel lateral
(hay una plantilla descargable). Describe entradas, capa, formato, estructura y modo de escritura; se combina
con lo descubierto automáticamente.

## Como API REST (v1.6)
`backend/main.py` ahora solo crea la app; la capa HTTP vive en `backend/api/`: contrato tipado (`/openapi.json`), autenticación por
`X-API-Key`, CORS configurable, límite de cuerpo y de solicitudes, cola con concurrencia limitada, cancelación
(`DELETE /api/v1/audits/{id}`), trabajos persistentes opcionales (SQLite), carga multipart del diccionario y errores sin eco de
secretos. Variables y ejemplos: [`docs/API_DESPLIEGUE.md`](docs/API_DESPLIEGUE.md). **Lea antes de desplegar:
[`docs/RIESGOS_API.md`](docs/RIESGOS_API.md).**

## Endpoints por dominio y Cloud Run (v1.7)
Un conjunto de rutas por análisis: `/api/v1/infrastructure`, `/api/v1/data-governance` y `/api/v1/architecture` (el antiguo
`/api/v1/audits` los ejecuta todos). Despliegue como microservicio con GitHub Actions: [`docs/DEPLOY_CLOUD_RUN.md`](docs/DEPLOY_CLOUD_RUN.md)
(workflow en `.github/workflows/deploy.yml`, bootstrap en `scripts/gcp_bootstrap_cloudrun.sh`). Pruebas de los endpoints: `api_tests/`.

## Puesta en marcha
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt

python -m uvicorn backend.main:app --port 8000
# Documentación interactiva: http://localhost:8000/docs
```

## Permisos (sólo lectura)
```bash
./scripts/grant_roles.sh <PROJECT_ID> credenciales.json                 # un proyecto
./scripts/grant_roles.sh <PROJECT_ID> credenciales.json <FOLDER_ID>     # carpeta completa
# Windows:  .\scripts\grant_roles.ps1 -ProjectId <ID> -KeyFile .\credenciales.json [-ParentId <FOLDER_ID>]
```
Asigna: `cloudasset.viewer`, `iam.securityReviewer`, `compute.viewer`, `monitoring.viewer`,
`bigquery.metadataViewer`, `bigquery.resourceViewer`, `bigquery.jobUser`, `bigquery.user`, `dataflow.viewer`,
`dataproc.viewer`, `composer.user`, `composer.environmentAndStorageObjectViewer`, `storage.objectViewer`,
`datastream.viewer`, `browser` (carpeta/organización) y `billing.viewer` en la cuenta de facturación; habilita las APIs.

Si falta un permiso, esa sección queda vacía, el resto continúa y el aviso indica el rol exacto.
El auditor ejecuta consultas a `INFORMATION_SCHEMA` con un tope de 20 GB facturados por consulta (normalmente
unos pocos MB) y excluye sus propios jobs del análisis.

## Decisiones de ingeniería
- **API Key por request** en la cabecera `X-LLM-API-Key`, inyectada en `config["configurable"]`; nunca entra en el
  estado, los logs ni las respuestas. Las credenciales cloud se escriben en un temporal `0600` y se borran siempre.
- **Ejecución en segundo plano**: la UI consulta el progreso cada 2 s; nunca queda bloqueada.
- **Guardrail anti-alucinación**: reglas de un catálogo cerrado por categoría y `resource_id` existentes.
- **Seguridad del HTML**: el código Mermaid generado por el LLM se pasa como cadena JSON a `mermaid.render`
  (`securityLevel: strict`), nunca se inyecta como HTML.
- **Costos**: precios de lista de GCP (us-central1) para comparar alternativas; con el export de facturación a
  BigQuery se muestra además el costo real por servicio.

## Tests
```bash
pytest -q
```
Sin red ni API Key: LLM falso y clientes de GCP simulados. Cubren el grafo completo, un escenario medallion
realista (Postgres → Datastream → bronze → silver → gold con conflictos, ciclos, sobrescritura de bronze, flujo
inactivo, HDFS en Dataproc), el parser de DAGs de Airflow, el dimensionamiento de clusters, el grafo de
arquitectura, la API de trabajos y que la API Key no se filtre.

## Modo demo
`DISCOVERY_MODE=mock python -m uvicorn backend.main:app` usa un inventario simulado.
