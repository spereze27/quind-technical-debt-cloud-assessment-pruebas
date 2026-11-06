#!/usr/bin/env bash
# Ejemplos con curl. Uso: BASE_URL=https://... API_KEY=... LLM_KEY=... PROJECT=mi-proyecto ./api_tests/curl_examples.sh [infrastructure|data-governance|architecture]
set -euo pipefail
BASE_URL="${BASE_URL:-http://localhost:8000}"; DOMAIN="${1:-infrastructure}"
: "${API_KEY:?}"; : "${LLM_KEY:?}"; : "${PROJECT:?}"
H=(-H "X-API-Key: $API_KEY" -H "X-LLM-API-Key: $LLM_KEY" -H "Content-Type: application/json")

curl -s "$BASE_URL/health" | python3 -m json.tool

JOB=$(curl -s -X POST "$BASE_URL/api/v1/$DOMAIN/audits" "${H[@]}" \
  -d "{\"provider\":\"GCP\",\"scope\":\"project\",\"target_id\":\"$PROJECT\",\"llm_provider\":\"${LLM_PROVIDER:-anthropic}\"}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['job_id'])")
echo "job_id=$JOB"

while :; do
  STATUS=$(curl -s "$BASE_URL/api/v1/$DOMAIN/audits/$JOB?include_raw=false" -H "X-API-Key: $API_KEY" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")
  echo "estado: $STATUS"; [[ "$STATUS" =~ ^(completed|error|cancelled)$ ]] && break; sleep 3
done
curl -s "$BASE_URL/api/v1/$DOMAIN/audits/$JOB?include_raw=false" -H "X-API-Key: $API_KEY" > "resultado_$DOMAIN.json"
python3 - <<PY
import json; r=json.load(open("resultado_$DOMAIN.json"))["result"] or {}
print("hallazgos:", len(r.get("findings", [])), "| deuda:", r.get("kpis", {}).get("overall_debt_score"))
open("informe_$DOMAIN.md","w").write(r.get("report") or "")
print("guardado: resultado_$DOMAIN.json e informe_$DOMAIN.md")
PY
