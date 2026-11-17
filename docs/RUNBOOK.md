# Operating the triage service

## Health
- `GET /healthz`: process is up.
- `GET /readyz`: cluster mode, retrieval mode (`hybrid` / `bm25`), loaded scenarios, models, and whether an LLM key is configured.
- `GET /metrics/summary`: rolling latency, cost per request, tokens/sec per agent, critic rejection rate, approval wait.
- Cloud Monitoring dashboard: `gcloud monitoring dashboards create --config-from-file=deploy/dashboards/cloud-monitoring-dashboard.json`

## Common operations
| Task | Command |
|---|---|
| Deploy / redeploy | `GEMINI_API_KEY=... ./scripts/deploy.sh` |
| Smoke test | `API_URL=... TRIAGE_API_KEY=... APPROVER_TOKEN=... ./scripts/smoke_test.sh` |
| Resume a failed investigation | `curl -X POST $URL/investigations/<id>/resume -H "x-api-key: $TRIAGE_API_KEY"` or `triage resume <id> --api $URL` |
| Approve a remediation | `triage approve <action_id> --api $URL --token $APPROVER_TOKEN` |
| Reject a remediation | `triage approve <action_id> --reject --api $URL --token $APPROVER_TOKEN` |
| Rotate tokens | edit `~/k8s-triage-agents-secrets.txt`, re-run `scripts/deploy.sh` (adds new secret versions, deploys `:latest`) |
| Re-record fixtures | `uv run python -m scripts.record_scenario` against kind |
| Rebuild the RAG index | `uv run python -m scripts.build_index` |

## Symptoms and fixes
| Symptom | Likely cause | Fix |
|---|---|---|
| Investigation `failed` with a 429/quota error | Gemini rate limit | wait, then `POST /investigations/{id}/resume`; completed steps are not repeated |
| `failed` with `max_llm_calls` | runaway loop | inspect steps; raise `MAX_LLM_CALLS` only if justified |
| `POST /investigations` returns 429 | per-instance rate limit (20/h) | expected guardrail |
| `retrieval_mode: bm25` in prod | image built without a fresh embedded index | re-run `deploy.sh` with `GEMINI_API_KEY` set |
| Approvals return 409 | already decided or expired (TTL 60 min) | re-run the investigation to get a fresh proposal |
| No traces in Cloud Trace | `TRACE_EXPORTER` not `gcp`, or SA lacks `cloudtrace.agent` | check env and IAM |
