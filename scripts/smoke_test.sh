#!/usr/bin/env bash
# Smoke-test a deployment: health, 3 replay investigations end to end, the
# approval gate, and (when GCP_PROJECT is set) traces in Cloud Trace.
#   API_URL=https://... TRIAGE_API_KEY=... APPROVER_TOKEN=... ./scripts/smoke_test.sh
set -euo pipefail
: "${API_URL:?}" "${TRIAGE_API_KEY:?}" "${APPROVER_TOKEN:?}"
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

curl -fsS "$API_URL/healthz" | j 'd["status"]'
curl -fsS "$API_URL/readyz" | j 'd["retrieval_mode"], d["scenarios"]'

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API_URL/investigations" \
  -H 'content-type: application/json' -d '{"symptom":"x y z","scenario":"oom-killed"}')
[[ "$code" == "401" ]] && echo "write without API key -> 401 OK" || { echo "expected 401, got $code"; exit 1; }

ids=()
for sc in bad-image-tag oom-killed broken-service-selector; do
  id=$(curl -fsS -X POST "$API_URL/investigations" -H "x-api-key: $TRIAGE_API_KEY" \
    -H 'content-type: application/json' -d "{\"symptom\":\"smoke test\",\"scenario\":\"$sc\"}" | j 'd["id"]')
  ids+=("$id"); echo "started $sc -> $id"
done
for id in "${ids[@]}"; do
  for _ in $(seq 1 90); do
    st=$(curl -fsS "$API_URL/investigations/$id" | j 'd["status"]')
    [[ "$st" == completed || "$st" == failed ]] && break; sleep 4
  done
  curl -fsS "$API_URL/investigations/$id" |
    j 'd["status"], (d["hypothesis"] or {}).get("category"), d["critic"]["final_verdict"], "$%.5f" % d["cost_usd"], d["trace_id"]'
done

action=$(curl -fsS "$API_URL/approvals" | j 'next((a["id"] for a in d if a["status"]=="pending"), "")')
if [[ -n "$action" ]]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API_URL/approvals/$action" \
    -H 'content-type: application/json' -d '{"approve":true}')
  [[ "$code" == "403" ]] && echo "approve without token -> 403 OK" || { echo "gate FAILED ($code)"; exit 1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API_URL/approvals/$action" \
    -H "x-approver-token: $TRIAGE_API_KEY" -H 'content-type: application/json' -d '{"approve":true}')
  [[ "$code" == "403" ]] && echo "approve with API key (not approver token) -> 403 OK" || { echo "gate FAILED ($code)"; exit 1; }
else
  echo "no pending remediation proposed in these runs (gate checked in tests)"
fi

if [[ -n "${GCP_PROJECT:-}" ]]; then
  trace=$(curl -fsS "$API_URL/investigations/${ids[0]}" | j 'd["trace_id"]')
  sleep 20
  curl -fsS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://cloudtrace.googleapis.com/v1/projects/$GCP_PROJECT/traces/$trace" |
    j '"cloud trace spans:", len(d.get("spans", []))'
fi
echo "smoke test passed"
