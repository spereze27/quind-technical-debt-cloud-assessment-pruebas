# Despliegue en Cloud Run con GitHub Actions

Servicio único (`--min-instances 1 --max-instances 1 --no-cpu-throttling`): los análisis corren en segundo plano **dentro del proceso**, por eso la CPU debe estar siempre asignada y no puede haber más de una instancia (ver `RIESGOS_API.md`).

## 1. Una sola vez en GCP (Cloud Shell, usuario Owner)

```bash
git clone https://github.com/OWNER/REPO && cd REPO
PROJECT_ID=mi-proyecto GITHUB_REPO=OWNER/REPO ./scripts/gcp_bootstrap_cloudrun.sh
```

Crea: APIs, repositorio de Artifact Registry, cuentas `gh-deployer` (despliega) y `tech-debt-runtime` (ejecuta y **audita con permisos de solo lectura**), el secreto `tech-debt-api-keys` en Secret Manager (imprime la API key generada) y el Workload Identity Pool restringido a su repositorio. Al final imprime los valores a cargar en GitHub.

Para auditar otros proyectos/carpetas: `./scripts/grant_roles.sh <proyecto> tech-debt-runtime@<PROJECT_ID>.iam.gserviceaccount.com <ID> folder|organization`.

## 2. Qué guardar en GitHub (Settings → Secrets and variables → Actions)

| Tipo | Nombre | Valor | Obligatorio |
|---|---|---|---|
| Secret | `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/<NÚMERO>/locations/global/workloadIdentityPools/github-pool/providers/github-provider` | Sí |
| Secret | `GCP_SERVICE_ACCOUNT` | `gh-deployer@<PROJECT_ID>.iam.gserviceaccount.com` | Sí |
| Secret | `SMOKE_API_KEY` | una de las API keys del servicio (para la prueba de humo posterior) | No |
| Variable | `GCP_PROJECT_ID` | ID del proyecto donde corre el servicio | Sí |
| Variable | `GCP_REGION` | p. ej. `us-central1` | Sí |
| Variable | `RUNTIME_SA_EMAIL` | `tech-debt-runtime@<PROJECT_ID>.iam.gserviceaccount.com` | Sí |
| Variable | `SERVICE_NAME` | por defecto `tech-debt-analyzer` | No |
| Variable | `AR_REPOSITORY` | por defecto `tech-debt` | No |
| Variable | `API_KEYS_SECRET_NAME` | por defecto `tech-debt-api-keys` | No |
| Variable | `CORS_ORIGINS` | solo si un navegador llamará directo a la API | No |

No se guardan en GitHub: claves JSON de cuentas de servicio (no existen, se usa WIF) ni la clave del LLM (la envía cada cliente en `X-LLM-API-Key`). Las API keys del servicio viven en Secret Manager y Cloud Run las monta como `API_KEYS`.

Opcional: cree el *Environment* `production` (Settings → Environments) para exigir aprobación manual antes de desplegar.

## 3. Cómo funciona el workflow (`.github/workflows/deploy.yml`)

| Evento | Qué hace |
|---|---|
| Pull request a `main` | solo `pytest` |
| Push a `main` | `pytest` → construye la imagen (`:SHA`) → la sube a Artifact Registry → `gcloud run deploy` → prueba de humo (`/health`, 401 sin clave) |
| Ejecución manual | igual que push, con opción de no desplegar |

Configuración que aplica en Cloud Run: 2 vCPU, 2 GiB, timeout 3600 s, concurrencia 40, `REQUIRE_AUTH=1`, `ADC_ENABLED=1`, `JOB_STORE=memory`, `API_KEYS` desde Secret Manager.

## 4. Identidad con la que se audita

Con `ADC_ENABLED=1`, si la solicitud **no** trae `cloud_credentials`, el servicio audita con la identidad de `tech-debt-runtime` (sin claves). Si la solicitud trae un JSON de Service Account, se usa ese (cliente que audita su propia nube). El servicio queda público a nivel de red (`--allow-unauthenticated`) y se protege con `X-API-Key`; para cerrarlo a nivel IAM quite esa opción y use `gcloud run services add-iam-policy-binding … roles/run.invoker`.

## 5. Probar tras el despliegue

```bash
URL=$(gcloud run services describe tech-debt-analyzer --region us-central1 --format='value(status.url)')
python api_tests/smoke_test.py --url $URL --api-key $API_KEY                       # sin gastar LLM
python api_tests/smoke_test.py --url $URL --api-key $API_KEY --llm-key $LLM_KEY \
       --llm-provider anthropic --project mi-proyecto --run architecture infrastructure data-governance
```

Guía completa: `docs/GUIA_PASO_A_PASO.md`. Más herramientas en `api_tests/`: `postman_collection.json` (importar en Postman; variables `base_url`, `api_key`, `llm_key`, `gcp_project`), `endpoints.http` (VS Code/JetBrains) y `curl_examples.sh`. Prueba local previa: `DISCOVERY_MODE=mock API_KEYS=k1 uvicorn backend.main:app` y `smoke_test.py --url http://localhost:8000 --api-key k1`.

## 6. Limitaciones propias de Cloud Run

| Tema | Detalle |
|---|---|
| Estado en memoria | Un reinicio o nuevo despliegue pierde los trabajos en curso y los Excel (`/tmp`). El cliente recibe 404 y debe reintentar. |
| Una instancia | No escala horizontalmente; los límites son `MAX_CONCURRENT_AUDITS` y `MAX_QUEUED_AUDITS`. Para escalar, mover cola y almacén a Cloud Tasks + Firestore/Cloud SQL (cambio de código). |
| Timeout | Máximo 60 min por solicitud; los endpoints síncronos y `stream` pueden cortarse antes por proxies. Use `/audits` + sondeo. |
| Memoria | `/tmp` cuenta como memoria; inventarios grandes con `include_raw=true` pesan varios MB. |
| Coste | `--min-instances 1` con CPU siempre asignada se factura de forma continua. |
| Clave del LLM inválida | El análisis termina en `completed` con avisos en `errors`; revise `errors` antes de confiar en el resultado. |
