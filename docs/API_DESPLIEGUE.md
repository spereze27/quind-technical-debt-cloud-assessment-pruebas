# Despliegue de la API

## Variables de entorno

| Variable | Defecto | Para qué sirve |
|---|---|---|
| `API_KEYS` | vacío (sin autenticación) | Claves válidas para `X-API-Key`, separadas por comas |
| `REQUIRE_AUTH` | `0` | Con `1`, el servicio no arranca si `API_KEYS` está vacío |
| `CORS_ORIGINS` | (vacío) | Orígenes permitidos, separados por comas (`*` abre a todos y desactiva cookies) |
| `MAX_BODY_MB` | `20` | Tamaño máximo del cuerpo (`413` si se excede) |
| `RATE_LIMIT_PER_MIN` | `20` | Auditorías que una clave puede crear por minuto (`0` = sin límite) |
| `MAX_CONCURRENT_AUDITS` | `2` | Auditorías que corren a la vez; el resto queda en `queued` |
| `MAX_QUEUED_AUDITS` | `20` | Tope de auditorías en cola y en curso (`429` si se excede) |
| `JOB_TIMEOUT_SECONDS` | `1800` | Tiempo máximo por auditoría (mínimo 30) |
| `JOB_TTL_SECONDS` | `3600` | Cuánto se conservan los trabajos terminados (mínimo 60) |
| `JOB_STORE` | `memory` | `memory` o `sqlite` |
| `JOBS_DB` | `jobs.sqlite3` | Archivo de SQLite (con `JOB_STORE=sqlite`) |
| `REPORTS_DIR` | carpeta temporal | Dónde se guardan los Excel (24 h) |
| `DISCOVERY_MODE` | `real` | `mock` usa un inventario simulado |

## Ejecución local

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
API_KEYS=mi-clave REQUIRE_AUTH=1 python -m uvicorn backend.main:app --port 8000 --workers 1
```

Documentación interactiva en `http://localhost:8000/docs` y contrato en `/openapi.json`.
Use siempre **un solo proceso** (`--workers 1`): los trabajos y la cola viven en el proceso (ver `docs/RIESGOS_API.md`, riesgo 1).

## Contenedor

```bash
docker build -t cloud-tech-debt-api .
docker run -p 8000:8000 -e API_KEYS=mi-clave -e REQUIRE_AUTH=1 -e CORS_ORIGINS=https://app.empresa.com cloud-tech-debt-api
```

El `Dockerfile` no se probó en este entorno (sin Docker disponible). En Cloud Run use
`--no-cpu-throttling --min-instances=1 --max-instances=1 --timeout=3600`.

## Llamadas

```bash
# crear
curl -s -X POST localhost:8000/api/v1/audits -H "Content-Type: application/json" \
  -H "X-API-Key: mi-clave" -H "X-LLM-API-Key: $ANTHROPIC_API_KEY" \
  -d '{"provider":"GCP","scope":"project","target_id":"mi-proyecto","llm_provider":"anthropic"}'
# consultar (sin el inventario crudo)
curl -s "localhost:8000/api/v1/audits/$JOB?include_raw=false" -H "X-API-Key: mi-clave"
# cancelar
curl -s -X DELETE localhost:8000/api/v1/audits/$JOB -H "X-API-Key: mi-clave"
# diccionario como archivo, sin Base64
curl -s -X POST localhost:8000/api/v1/audits/multipart -H "X-API-Key: mi-clave" -H "X-LLM-API-Key: $KEY" \
  -F 'request={"provider":"GCP","scope":"project","target_id":"mi-proyecto","profile_datasets":["credito"]}' \
  -F "dictionary=@DiccionarioDatos.xlsx"
```

Los Excel exigen también `X-API-Key`: descárguelos con `fetch` y la cabecera, no con un enlace directo.

## Estructura

```
backend/
  main.py            # app = create_app()
  api/
    app.py           # fábrica, CORS, middleware, ciclo de vida
    settings.py      # variables de entorno
    schemas.py       # contrato: entrada y salida tipadas
    security.py      # X-API-Key y límite de solicitudes
    errors.py        # formato único {detail, code, request_id}, sin eco de valores
    middleware.py    # X-Request-ID y límite de tamaño
    service.py       # validación, credenciales temporales, ejecución del grafo
    jobs.py          # cola, cancelación, tiempo máximo, almacenes memory/sqlite
    routes/          # audits.py y misc.py (salud y descarga de Excel)
  agent_graph/, core/   # sin cambios (LangGraph, KPIs, plataforma de datos)
```


## Endpoints por dominio (v1.7)

| Dominio | Prefijo | Qué analiza | Resultado principal |
|---|---|---|---|
| Infraestructura | `/api/v1/infrastructure` | seguridad, gobierno, costos | `findings`, `kpis` (3 categorías), `report` |
| Gobierno de datos | `/api/v1/data-governance` | BigQuery/lake, flujos, capacidad, diccionario | `findings`, `kpis` (3 categorías), `report`, `excel_report` |
| Arquitectura | `/api/v1/architecture` | componentes, flujos, identidades, exposición | `architecture{summary,nodes,edges,flows,risks}`, `report` |

Cada prefijo ofrece `POST /audits` (202), `GET /audits/{id}`, `DELETE /audits/{id}`, `POST /audit` (síncrono) y `POST /audit/stream` (NDJSON); `data-governance` añade `POST /audits/multipart`. Un `job_id` solo se puede consultar en el prefijo que lo creó (otro → 404). `/api/v1/audits` (los tres dominios en un trabajo) se mantiene por compatibilidad.

Campos por dominio: `infrastructure` = provider, scope, target_id, llm_provider, llm_model, cloud_credentials, billing_export_table. `architecture` = los mismos sin `billing_export_table`. `data-governance` = los de infraestructura + history_days, sample_lake, read_dags, max_projects, flow_catalog, data_dictionary_b64, data_dictionary_name, profile_project, profile_datasets, profile_max_gb. Un campo que no pertenece al dominio devuelve 422.

Despliegue en Cloud Run: `docs/DEPLOY_CLOUD_RUN.md`. Pruebas: `api_tests/`.

### Esperar el resultado en una sola llamada (v1.7)

| Llamada | Efecto |
|---|---|
| `POST /api/v1/{dominio}/audits?wait=true&wait_timeout=3300` | Espera hasta `wait_timeout` s. `200` con el estado y el `result` si terminó; `202` con el estado actual si no (el trabajo sigue). `include_raw=false` por defecto aquí. |
| `GET /api/v1/{dominio}/audits/{id}?wait=600` | Espera hasta 600 s a que el trabajo termine antes de responder (long-poll). |

Si el cliente se desconecta durante la espera, el análisis **no se cancela**. Máximo 3300 s, por debajo del límite de 3600 s de Cloud Run.
