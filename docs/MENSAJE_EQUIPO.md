# Mensaje para el equipo: cómo probar la API de deuda técnica

Hola equipo. Estas son las rutas del servicio y todo lo que necesitan para probarlo.

**URL base:** `https://tech-debt-analyzer-871145895348.us-central1.run.app`
Documentación interactiva (todos los campos): `<URL base>/docs`

| Análisis | Ruta (POST) |
|---|---|
| Infraestructura (seguridad, gobierno, costos) | `/api/v1/infrastructure/audits` |
| Arquitectura (componentes, flujos, identidades) | `/api/v1/architecture/audits` |
| Gobierno de datos (BigQuery, lake, flujos, capacidad) | `/api/v1/data-governance/audits` |

Agregando `?wait=true` la misma llamada espera a que termine el análisis y devuelve el resultado. No hace falta consultar aparte ni programar esperas.

## 0. Qué deben pedir antes de empezar

1. **API key del servicio** (la entrega el administrador; va en la cabecera `X-API-Key`).
2. **Clave de un LLM** propia: Anthropic, OpenAI o Gemini (cabecera `X-LLM-API-Key`). Cada llamada gasta cuota de esa clave.
3. El **ID del proyecto de GCP** que se quiere analizar.

## 1. En la nube que se va a analizar: crear una cuenta de solo lectura

Abran **Cloud Shell** (https://shell.cloud.google.com) con un usuario Owner del proyecto a analizar y peguen:

```bash
export TARGET=ID_DEL_PROYECTO_A_ANALIZAR
gcloud config set project $TARGET
SA=tech-debt-auditor

gcloud services enable cloudasset.googleapis.com cloudresourcemanager.googleapis.com compute.googleapis.com \
  iam.googleapis.com monitoring.googleapis.com cloudbilling.googleapis.com billingbudgets.googleapis.com \
  bigquery.googleapis.com bigquerydatatransfer.googleapis.com

gcloud iam service-accounts create $SA --display-name "Auditor de deuda tecnica (solo lectura)"
for ROLE in roles/cloudasset.viewer roles/iam.securityReviewer roles/compute.viewer roles/monitoring.viewer \
  roles/bigquery.metadataViewer roles/bigquery.resourceViewer roles/bigquery.jobUser roles/bigquery.user \
  roles/dataflow.viewer roles/dataproc.viewer roles/composer.user roles/composer.environmentAndStorageObjectViewer \
  roles/storage.objectViewer roles/datastream.viewer; do
  gcloud projects add-iam-policy-binding $TARGET --member "serviceAccount:$SA@$TARGET.iam.gserviceaccount.com" \
    --role $ROLE --condition=None --quiet >/dev/null && echo "OK $ROLE"
done
```

Todos son roles de **lectura** (el rol `bigquery.jobUser` solo permite ejecutar consultas sobre metadatos). Los permisos tardan 1 a 2 minutos en propagarse. Si analizan una carpeta u organización, además necesitan `roles/browser` en ese nivel. El repositorio trae `scripts/grant_roles.sh`, que hace lo mismo.

## 2. Descargar el `sa.json` de esa cuenta

```bash
gcloud iam service-accounts keys create sa.json --iam-account $SA@$TARGET.iam.gserviceaccount.com
cloudshell download sa.json
```

Si la organización bloquea la creación de claves (política `iam.disableServiceAccountKeyCreation`), pidan al administrador del servicio que dé estos mismos roles a la cuenta `tech-debt-runtime@<proyecto-donde-corre-el-servicio>.iam.gserviceaccount.com`; entonces no se envía `sa.json` y se omite `cloud_credentials`.

**El `sa.json` es una llave:** no lo suban a Git, ni lo peguen en chats, y bórrenlo al terminar (`gcloud iam service-accounts keys delete ...`).

## 3. Preparar la terminal (necesitan `curl` y `jq`; Cloud Shell ya los trae)

Suban `sa.json` a la carpeta donde van a trabajar y definan:

```bash
export URL=https://tech-debt-analyzer-871145895348.us-central1.run.app
export API_KEY=...            # la que les dieron
export LLM_KEY=...            # su clave del LLM
export PROJECT=ID_DEL_PROYECTO_A_ANALIZAR
export LLM=gemini             # anthropic | openai | gemini
```

No guarden el contenido de `sa.json` en una variable de entorno: es demasiado grande y `curl` falla con "La lista de argumentos es demasiado larga". Los comandos de abajo lo leen directamente del archivo.

## 4. Los curl (cada uno guarda su resultado)

**Infraestructura** → `result_infra.json`
```bash
jq -n --arg p "$PROJECT" --arg l "$LLM" --rawfile c sa.json '{
  provider:"GCP", scope:"project", target_id:$p, llm_provider:$l, cloud_credentials:$c,
  llm_model:null, billing_export_table:null }' |
curl -sS --max-time 3400 -o result_infra.json -w "HTTP %{http_code}\n" -X POST "$URL/api/v1/infrastructure/audits?wait=true" \
  -H "X-API-Key: $API_KEY" -H "X-LLM-API-Key: $LLM_KEY" -H "Content-Type: application/json" -d @-
```

**Arquitectura** → `result_arqui.json`
```bash
jq -n --arg p "$PROJECT" --arg l "$LLM" --rawfile c sa.json '{
  provider:"GCP", scope:"project", target_id:$p, llm_provider:$l, cloud_credentials:$c, llm_model:null }' |
curl -sS --max-time 3400 -o result_arqui.json -w "HTTP %{http_code}\n" -X POST "$URL/api/v1/architecture/audits?wait=true" \
  -H "X-API-Key: $API_KEY" -H "X-LLM-API-Key: $LLM_KEY" -H "Content-Type: application/json" -d @-
```

**Gobierno de datos** → `result_data.json`
```bash
jq -n --arg p "$PROJECT" --arg l "$LLM" --rawfile c sa.json '{
  provider:"GCP", scope:"project", target_id:$p, llm_provider:$l, cloud_credentials:$c, llm_model:null,
  billing_export_table:null, history_days:30, sample_lake:true, read_dags:true, max_projects:10, flow_catalog:null }' |
curl -sS --max-time 3400 -o result_data.json -w "HTTP %{http_code}\n" -X POST "$URL/api/v1/data-governance/audits?wait=true" \
  -H "X-API-Key: $API_KEY" -H "X-LLM-API-Key: $LLM_KEY" -H "Content-Type: application/json" -d @-
```

Cada dominio acepta **solo sus campos**; uno que no le corresponde devuelve 422 (por ejemplo `history_days` en arquitectura). Mientras no esté el módulo de diccionario, no envíen `data_dictionary_b64` (devuelve 501).

Pueden lanzar los tres a la vez (agreguen `&` al final de cada curl, o usen `./api_tests/run_audits.sh`, que además deja todo en `./resultados/`). La API ejecuta 2 análisis simultáneos y deja el resto en cola.

**Códigos de respuesta con `wait=true`:** `200` terminó (revisen `status`); `202` aún corre, porque venció el tiempo de espera (`wait_timeout`, hasta 3300 s). En ese caso continúen con:
```bash
curl -sS -o result_infra.json "$URL/api/v1/infrastructure/audits/$(jq -r .job_id result_infra.json)?wait=600" -H "X-API-Key: $API_KEY"
```

## 5. Revisar el resultado

```bash
jq '{status, discovery: .result.discovery_status, hallazgos: (.result.findings|length), deuda: .result.kpis.overall_debt_score}' result_infra.json
jq -r '.result.errors[]' result_infra.json          # avisos: aquí aparece cualquier problema
jq -r '.result.report' result_infra.json > informe_infra.md
jq '.result.architecture.risks' result_arqui.json
```

Un resultado es confiable cuando `status` es `completed`, `discovery` es `complete` y `errors` está vacío. Un `completed` con avisos significa que parte del análisis no se hizo.

## 6. Si algo falla

| Síntoma | Causa y solución |
|---|---|
| `401 unauthorized` | Falta o es incorrecta la cabecera `X-API-Key`. |
| `401 missing_llm_key` | Falta la cabecera `X-LLM-API-Key`. |
| `422 Extra inputs are not permitted` | Un campo no pertenece a ese dominio; usen solo los de su curl. |
| `404 Auditoría no encontrada` | El `job_id` es de otro dominio, está vencido (1 h) o el servicio se reinició. Repitan el análisis. |
| `429` | Demasiadas solicitudes por minuto o cola llena; esperen unos segundos (ver `Retry-After`). |
| `discovery_status: partial` y "PERMISO DENEGADO" en `errors` | Falta un rol en la cuenta de solo lectura; el mensaje indica cuál. Concédanlo y repitan. |
| Error 401 o 400 del proveedor del LLM en `errors` | La clave del LLM es inválida o no tiene cuota; el análisis termina sin hallazgos. |
| `501` en gobierno de datos | Se envió el diccionario; esa función aún no está en este despliegue. |
| `curl: (28)` por tiempo | Se superó `--max-time`; consulten con el `GET ...?wait=600` de arriba. |
