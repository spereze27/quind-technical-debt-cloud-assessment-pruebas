#!/usr/bin/env bash
# Lanza los 3 análisis (arquitectura, infraestructura, datos) EN PARALELO, espera a que terminen y guarda los resultados.
# Uso:
#   export URL=https://...run.app API_KEY=... LLM_KEY=... PROJECT=mi-proyecto
#   export LLM_PROVIDER=gemini          # anthropic | openai | gemini
#   export SA_JSON=sa.json              # opcional: auditar con esta cuenta de servicio (si no, la del servicio)
#   ./api_tests/run_audits.sh [dominio ...]       # por defecto los 3
# Salida en ./resultados/: <dominio>.json, <dominio>_informe.md y un resumen en pantalla.
set -uo pipefail
: "${URL:?}"; : "${API_KEY:?}"; : "${LLM_KEY:?}"; : "${PROJECT:?}"
LLM_PROVIDER="${LLM_PROVIDER:-anthropic}"; TIMEOUT="${TIMEOUT:-1800}"
DOMAINS=("$@"); [[ ${#DOMAINS[@]} -eq 0 ]] && DOMAINS=(architecture infrastructure data-governance)
OUT=resultados; mkdir -p "$OUT"

# Cuerpo base (lo comparten los tres dominios). Las credenciales NUNCA van en variables de entorno: se leen del archivo.
base_body() {
  if [[ -n "${SA_JSON:-}" ]]; then
    jq -n --arg p "$PROJECT" --arg l "$LLM_PROVIDER" --rawfile c "$SA_JSON" \
      '{provider:"GCP",scope:"project",target_id:$p,llm_provider:$l,cloud_credentials:$c}'
  else
    jq -n --arg p "$PROJECT" --arg l "$LLM_PROVIDER" '{provider:"GCP",scope:"project",target_id:$p,llm_provider:$l}'
  fi
}
# Cada dominio acepta SOLO sus campos: los extras devuelven 422.
body_for() {
  case "$1" in
    data-governance) base_body | jq --argjson d "${HISTORY_DAYS:-30}" '. + {history_days:$d}' ;;
    *) base_body ;;
  esac
}

run_one() {
  local d="$1" f="$OUT/$1" t0=$SECONDS
  body_for "$d" > "$f.request.json"
  # wait=true: la propia API espera a que termine y devuelve el resultado en la misma llamada
  local code
  code=$(curl -sS -o "$f.json" -w '%{http_code}' --max-time $((TIMEOUT + 120)) -X POST \
         "$URL/api/v1/$d/audits?wait=true&wait_timeout=$TIMEOUT" -H "X-API-Key: $API_KEY" \
         -H "X-LLM-API-Key: $LLM_KEY" -H "Content-Type: application/json" -d @"$f.request.json")
  if [[ "$code" != "200" && "$code" != "202" ]]; then echo "[$d] ERROR (HTTP $code): $(cat "$f.json")"; return 1; fi
  local st; st=$(jq -r .status "$f.json")
  while [[ ! "$st" =~ ^(completed|error|cancelled)$ ]] && (( SECONDS - t0 < TIMEOUT )); do   # solo si venció wait_timeout
    curl -sS "$URL/api/v1/$d/audits/$(jq -r .job_id "$f.json")?include_raw=false&wait=300" -H "X-API-Key: $API_KEY" > "$f.json"
    st=$(jq -r '.status // "?"' "$f.json")
  done
  jq -r '.result.report // ""' "$f.json" > "${f}_informe.md"
  echo "[$d] FIN: $st en $((SECONDS - t0)) s | descubrimiento=$(jq -r '.result.discovery_status' "$f.json")" \
       "| hallazgos=$(jq '.result.findings|length' "$f.json") | deuda=$(jq -r '.result.kpis.overall_debt_score // "n/a"' "$f.json")" \
       "| avisos=$(jq '.result.errors|length' "$f.json")"
}

pids=(); for d in "${DOMAINS[@]}"; do run_one "$d" & pids+=($!); done
rc=0; for p in "${pids[@]}"; do wait "$p" || rc=1; done
echo; echo "Avisos (errors) por dominio:"; for d in "${DOMAINS[@]}"; do
  echo "== $d"; jq -r '.result.errors[]? | "  - " + (.[0:200])' "$OUT/$d.json" 2>/dev/null; done
exit $rc
