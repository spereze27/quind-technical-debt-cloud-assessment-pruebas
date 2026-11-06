#!/usr/bin/env bash
# Un dominio, una sola llamada curl: la API espera y devuelve el resultado (wait=true).
# Uso: URL=... API_KEY=... LLM_KEY=... PROJECT=... [SA_JSON=sa.json] [LLM_PROVIDER=gemini] ./api_tests/curl_examples.sh infrastructure|architecture|data-governance
set -euo pipefail
: "${URL:?}"; : "${API_KEY:?}"; : "${LLM_KEY:?}"; : "${PROJECT:?}"
D="${1:-infrastructure}"; OUTF="result_${D}.json"
BODY=$(jq -n --arg p "$PROJECT" --arg l "${LLM_PROVIDER:-anthropic}" '{provider:"GCP",scope:"project",target_id:$p,llm_provider:$l}')
[[ -n "${SA_JSON:-}" ]] && BODY=$(jq -n --argjson b "$BODY" --rawfile c "$SA_JSON" '$b + {cloud_credentials:$c}')
curl -sS --max-time 3400 -o "$OUTF" -w "HTTP %{http_code}\n" -X POST "$URL/api/v1/$D/audits?wait=true" \
  -H "X-API-Key: $API_KEY" -H "X-LLM-API-Key: $LLM_KEY" -H "Content-Type: application/json" -d "$BODY"
jq '{status, discovery: .result.discovery_status, hallazgos: (.result.findings|length), avisos: .result.errors}' "$OUTF"
