#!/usr/bin/env bash
# Prepara GCP para desplegar con GitHub Actions (una sola vez). Ejecutar en Cloud Shell con un usuario Owner del proyecto.
# Uso:  PROJECT_ID=mi-proyecto GITHUB_REPO=mi-org/mi-repo ./scripts/gcp_bootstrap_cloudrun.sh
# Opcionales: REGION (us-central1) SERVICE_NAME AR_REPO POOL_ID PROVIDER_ID
set -euo pipefail
: "${PROJECT_ID:?define PROJECT_ID}"; : "${GITHUB_REPO:?define GITHUB_REPO como owner/repo}"
REGION="${REGION:-us-central1}"; SERVICE_NAME="${SERVICE_NAME:-tech-debt-analyzer}"; AR_REPO="${AR_REPO:-tech-debt}"
POOL_ID="${POOL_ID:-github-pool}"; PROVIDER_ID="${PROVIDER_ID:-github-provider}"
DEPLOYER_SA="gh-deployer"; RUNTIME_SA="tech-debt-runtime"; SECRET_NAME="${SECRET_NAME:-tech-debt-api-keys}"
DEPLOYER_EMAIL="${DEPLOYER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"; RUNTIME_EMAIL="${RUNTIME_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud config set project "$PROJECT_ID" >/dev/null

echo "1/7 APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com iam.googleapis.com cloudresourcemanager.googleapis.com sts.googleapis.com

echo "2/7 Artifact Registry"
gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location "$REGION" --description="Imágenes de $SERVICE_NAME"

echo "3/7 Cuentas de servicio (despliegue y ejecución)"
for sa in "$DEPLOYER_SA" "$RUNTIME_SA"; do
  gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "$sa" --display-name "$sa"
done
for role in roles/run.admin roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$DEPLOYER_EMAIL" --role "$role" --condition=None --quiet >/dev/null
done
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_EMAIL" --member "serviceAccount:$DEPLOYER_EMAIL" \
  --role roles/iam.serviceAccountUser --quiet >/dev/null

echo "4/7 Permisos de LECTURA que usa el servicio para auditar (este proyecto)"
./scripts/grant_roles.sh "$PROJECT_ID" "$RUNTIME_EMAIL"
echo "    Para auditar OTROS proyectos/carpetas: ./scripts/grant_roles.sh <proyecto> $RUNTIME_EMAIL <ID> folder|organization"

echo "5/7 Secret Manager: API keys del servicio"
if ! gcloud secrets describe "$SECRET_NAME" >/dev/null 2>&1; then
  API_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
  printf '%s' "$API_KEY" | gcloud secrets create "$SECRET_NAME" --data-file=- --replication-policy=automatic
  echo "    >>> API KEY GENERADA (guárdala ahora, no se vuelve a mostrar): $API_KEY"
else
  echo "    El secreto ya existe; para rotar: printf 'nueva1,nueva2' | gcloud secrets versions add $SECRET_NAME --data-file=-"
fi
gcloud secrets add-iam-policy-binding "$SECRET_NAME" --member "serviceAccount:$RUNTIME_EMAIL" \
  --role roles/secretmanager.secretAccessor --quiet >/dev/null

echo "6/7 Workload Identity Federation (GitHub → GCP, sin claves)"
gcloud iam workload-identity-pools describe "$POOL_ID" --location global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$POOL_ID" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" --location global --workload-identity-pool "$POOL_ID" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" --location global --workload-identity-pool "$POOL_ID" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository=='${GITHUB_REPO}'"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_EMAIL" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_REPO}" --quiet >/dev/null
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"

echo "7/7 Listo. Cargue esto en GitHub (Settings → Secrets and variables → Actions):"
cat <<EOF

  SECRETS (pestaña Secrets)
    GCP_WORKLOAD_IDENTITY_PROVIDER = ${WIF_PROVIDER}
    GCP_SERVICE_ACCOUNT            = ${DEPLOYER_EMAIL}
    SMOKE_API_KEY                  = (opcional) la API key generada arriba

  VARIABLES (pestaña Variables)
    GCP_PROJECT_ID      = ${PROJECT_ID}
    GCP_REGION          = ${REGION}
    SERVICE_NAME        = ${SERVICE_NAME}
    AR_REPOSITORY       = ${AR_REPO}
    RUNTIME_SA_EMAIL    = ${RUNTIME_EMAIL}
    API_KEYS_SECRET_NAME= ${SECRET_NAME}
    CORS_ORIGINS        = (opcional; solo si un navegador llamará a la API directamente)
EOF
