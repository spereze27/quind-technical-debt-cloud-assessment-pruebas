# Guía paso a paso: probar, desplegar y verificar

Requisitos: cuenta GCP con un proyecto con facturación, usuario Owner de ese proyecto, repositorio en GitHub, una clave de LLM (Anthropic, OpenAI o Gemini).

## Parte A. Probar en local (sin nube, sin gastar LLM real)

1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements-api.txt pytest httpx`
3. `python -m pytest -q tests` → debe decir `73 passed`.
4. Levantar la API con datos simulados:
   `DISCOVERY_MODE=mock REQUIRE_AUTH=1 API_KEYS=clave-local uvicorn backend.main:app --port 8000`
5. En otra terminal: `python api_tests/smoke_test.py --url http://localhost:8000 --api-key clave-local`
   → debe terminar en `RESULTADO: TODO OK`. Documentación interactiva: http://localhost:8000/docs

## Parte B. Subir el código a GitHub

1. Cree un repositorio vacío en GitHub (p. ej. `mi-org/tech-debt-analyzer`).
2. En la carpeta del proyecto: `git init && git add . && git commit -m "API v1.7" && git branch -M main && git remote add origin https://github.com/mi-org/tech-debt-analyzer.git && git push -u origin main`
   (el primer push dispara el workflow y fallará hasta completar la Parte C; es normal).

## Parte C. Preparar GCP (una sola vez, en Cloud Shell)

1. Abra https://shell.cloud.google.com, y ejecute `gcloud config set project MI_PROYECTO`.
2. `git clone https://github.com/mi-org/tech-debt-analyzer && cd tech-debt-analyzer`
3. `PROJECT_ID=MI_PROYECTO GITHUB_REPO=mi-org/tech-debt-analyzer ./scripts/gcp_bootstrap_cloudrun.sh`
4. **Copie y guarde la API KEY** que imprime el paso 5/7 (no se vuelve a mostrar).
5. Copie el bloque final “SECRETS / VARIABLES” que imprime el script.

## Parte D. Configurar GitHub

En el repositorio: Settings → Secrets and variables → Actions.

| Pestaña | Nombre | Valor |
|---|---|---|
| Secrets | `GCP_WORKLOAD_IDENTITY_PROVIDER` | el que imprimió el script |
| Secrets | `GCP_SERVICE_ACCOUNT` | `gh-deployer@MI_PROYECTO.iam.gserviceaccount.com` |
| Secrets | `SMOKE_API_KEY` | la API key (opcional) |
| Variables | `GCP_PROJECT_ID`, `GCP_REGION`, `RUNTIME_SA_EMAIL` | los que imprimió el script |
| Variables | `SERVICE_NAME`, `AR_REPOSITORY`, `API_KEYS_SECRET_NAME`, `CORS_ORIGINS` | opcionales (hay valores por defecto) |

## Parte E. Desplegar

1. Pestaña **Actions** → workflow `deploy-cloud-run` → *Run workflow* (o haga un push a `main`).
2. Espere los pasos: test → build → push → deploy → prueba de humo. Debe quedar en verde (≈ 5–8 min).
3. Obtenga la URL: `gcloud run services describe tech-debt-analyzer --region us-central1 --format='value(status.url)'`

Si falla: `Permission denied` en auth → revise `GCP_WORKLOAD_IDENTITY_PROVIDER` y que `GITHUB_REPO` del bootstrap coincida con el repositorio; `secret not found` → nombre en `API_KEYS_SECRET_NAME`; cualquier otro, vea el log del paso rojo.

## Parte F. Probar el servicio desplegado

```bash
export URL=https://....run.app API_KEY=la-clave-generada LLM_KEY=sk-ant-... PROJECT=MI_PROYECTO
curl $URL/health                                         # status ok, auth_enabled true, 3 dominios
python api_tests/smoke_test.py --url $URL --api-key $API_KEY            # validaciones sin gastar LLM
python api_tests/smoke_test.py --url $URL --api-key $API_KEY --llm-key $LLM_KEY \
   --llm-provider anthropic --project $PROJECT --run architecture infrastructure data-governance
```

Para el equipo que probará la API: `docs/MENSAJE_EQUIPO.md` (cuenta de solo lectura, sa.json y curls). Manual con curl (cambie el dominio por `infrastructure`, `data-governance` o `architecture`): `./api_tests/curl_examples.sh architecture`.
Con Postman: importe `api_tests/postman_collection.json`, complete las variables `base_url`, `api_key`, `llm_key`, `gcp_project` y ejecute las carpetas en orden (Crear → Estado).

Qué debe ver: `POST …/audits` → 202 con `job_id`; `GET …/audits/{id}` pasa de `queued/running` a `completed`; `result.report` trae el informe en Markdown; en `infrastructure` y `data-governance`, `result.findings` y `result.kpis`; en `architecture`, `result.architecture.risks`.

## Parte G. Operación

| Tarea | Comando |
|---|---|
| Ver logs | `gcloud run services logs read tech-debt-analyzer --region us-central1 --limit 100` |
| Rotar API keys | `printf 'nueva1,nueva2' \| gcloud secrets versions add tech-debt-api-keys --data-file=-` y redesplegar |
| Auditar otros proyectos | `./scripts/grant_roles.sh OTRO_PROYECTO tech-debt-runtime@MI_PROYECTO.iam.gserviceaccount.com` |
| Apagar para no pagar | `gcloud run services delete tech-debt-analyzer --region us-central1` |
