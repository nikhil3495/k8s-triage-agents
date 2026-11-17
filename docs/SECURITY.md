# Security model

## Principles
1. **Agents can read, never write.** Investigation tools use a `ClusterReader`. The live identity is bound to `deploy/k8s-rbac/investigator-readonly.yaml`: get/list/watch only, and no Secrets.
2. **Remediation requires a human.** Agents can only *propose* (`propose_remediation`). Execution happens only in `ApprovalService.decide()` after:
   - a constant-time check of the **approver token**, which is separate from the API key (`X-Approver-Token`),
   - a status check (still `pending`, so no double execution),
   - an expiry check (default TTL 60 min).
   The executor's cluster identity is a separate, namespaced Role (`deploy/k8s-rbac/remediator-namespaced.yaml`): patch/scale Deployments and read ReplicaSets, nothing else.
3. **Only allowlisted actions:** `rollout_restart`, `scale_deployment` (0-50), `rollback_deployment`, `patch_resources` (cpu/memory only). Anything else is rejected at proposal time.
4. **Everything is audited:** proposed, denied_bad_token, approved, rejected, expired, executed, failed (`audit_log` table, `GET /approvals/{id}/audit`).

Tests enforcing this: `tests/unit/test_hitl.py` (token, reject, expiry, double-approve, an AST scan proving no agent or tool module references a writer or the executor) and `tests/unit/test_api.py` (HTTP 401/403/409 paths, and the API key rejected as an approver token).

## Secrets
- Nothing is committed: `.env` is gitignored and `.env.example` has placeholders. `scripts/secret_scan.py` runs before every push, and gitleaks runs in CI.
- In Cloud Run: `GEMINI_API_KEY`, `TRIAGE_API_KEY`, `APPROVER_TOKEN` and `SESSION_DB_URL` come from Secret Manager (`--set-secrets`). The runtime service account has only `cloudtrace.agent`, `monitoring.metricWriter`, `logging.logWriter`, `secretmanager.secretAccessor` and `aiplatform.user` (for the Agent Engine session store).
- Generated tokens are written to `~/k8s-triage-agents-secrets.txt` (mode 600, outside the repo).
- The embedding index is built with the key *outside* the image, so no key enters an image layer.
- CI/CD uses Workload Identity Federation (`scripts/setup_wif.sh`), with no JSON keys.

## Data exposure to the LLM
- Pod env **values** are dropped during normalization. Only names and `valueFrom` references are kept.
- Logs are tail-limited (100 lines / 4000 chars).
- Tool outputs are capped at 50 items.

## Public demo
`--allow-unauthenticated` exposes the UI and read endpoints. Write endpoints still need the API key, and approvals need the approver token. The demo runs in replay mode with a **simulated** writer, so approving there changes no cluster. A per-instance rate limit (20 investigations per hour), `max_llm_calls`, tool budgets and `max-instances=2` bound spend.

## Known gaps
- API keys are static bearer secrets. Production should put IAP or OIDC in front and map approvers to identities.
- Prompt injection through log contents is mitigated (read-only tools, human approval, allowlisted actions) but not eliminated. A malicious log line could still steer the *hypothesis*, which is why the critic and the human gate exist.
