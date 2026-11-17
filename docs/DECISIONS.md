# Decisions

Every non-obvious choice made while building this, with the reason. Dates are when the decision was made.

## D1 - Google ADK 2.14.1, read from source (2026-11-17)
The installed `google-adk` was 2.14.1, newer than any documentation memory, so every API used here was read from the installed package: `LlmAgent` fields and callbacks, `AgentTool` (runs the sub-agent in a nested `Runner` with its own in-memory session, forwards state deltas back to the parent), `App` + `ResumabilityConfig`, `Runner.run_async(invocation_id=...)`, `DatabaseSessionService`, `BasePlugin`, `PlanReActPlanner`, `LlmCapabilities.output_schema_and_tools`.

## D2 - Hierarchy: `AgentTool` for workers, `LoopAgent` for the critic loop (2026-11-17)
- **Workers as `AgentTool`s, not `sub_agents` transfer.** With `sub_agents`, the LLM *transfers control* and the supervisor loses the thread. With `AgentTool`, the supervisor calls a specialist like a function, gets its findings back as an observation and keeps planning. That is what "supervisor delegates and merges findings" needs.
- **Critic loop as a `LoopAgent([Supervisor, Critic, VerdictGate])`.** `VerdictGate` is a tiny custom `BaseAgent` that escalates (ends the loop) on `PASS`. On `REVISE` the loop runs the supervisor again, and its dynamic instruction includes the critic's reasons and missing evidence. `max_iterations = MAX_CRITIC_ROUNDS` (default 2) bounds cost.
- ADK 2.14 marks `LoopAgent` as deprecated in favour of `Workflow`. It still works, it supports resumability (tested), and the warning itself says a `Workflow` "cannot yet be used as an LlmAgent sub-agent". Migrating to `Workflow` is on the roadmap.

## D3 - ReAct = ADK `PlanReActPlanner` + budgets (2026-11-17)
Workers use ADK's built-in `PlanReActPlanner` (plan, reason, act, observe). Guardrails:
- **Per-agent tool budget:** `before_tool_callback` counts calls per (invocation, agent) in session state. Over budget, the tool returns an error observation telling the model to answer now. This is softer than raising, so the loop still produces a final answer.
- **Run-wide cap:** `RunConfig(max_llm_calls=MAX_LLM_CALLS)`.
- **Tools never raise into the loop.** Any exception becomes an `{"error": ...}` observation. This was added after the recorded `unschedulable` fixture showed a pending pod's logs recorded as `null` crashing the run.

## D4 - Models (2026-11-17)
The Gemini pricing page was checked on 2026-11-17. `gemini-3.5-flash` (ADK's default) is no longer listed. The page lists `gemini-3.8-flash` (current Flash) and `gemini-3.1-flash-lite` (cheapest).
- `SUPERVISOR_MODEL=gemini-3.8-flash` and `CRITIC_MODEL=gemini-3.8-flash`: the reasoning-heavy roles.
- `WORKER_MODEL=gemini-3.1-flash-lite`: tool-calling workers, $0.25 / $1.50 per 1M tokens.
- **Not verified with a live `models.list` call**, because no `GEMINI_API_KEY` was available during the build. All three are env-configurable.

## D5 - Kubernetes access: one `ClusterReader` protocol, two implementations
`LiveClusterClient` (official `kubernetes` client) and `ReplayClusterClient` (JSON snapshots). Both return the **same normalized dicts** because the recorder (`scripts/record_scenario.py`, `eval.run_eval --record`) captures fixtures *through* `LiveClusterClient`. Replay is therefore a faithful stand-in, not hand-written mock data. The 8 fixtures in `eval/fixtures/` were recorded from a kind v1.33.1 cluster on 2026-11-17. The only hand-written snapshot lives in `tests/conftest.py` and is labelled `synthetic-unit-test`.
- Env *values* are stripped during normalization (only names and `valueFrom` refs are kept), so secrets in pod specs never reach the LLM.

## D6 - HITL: decoupled approval queue rather than ADK tool confirmation
ADK has `FunctionTool(require_confirmation=True)`, but its confirmation is answered *inside the same invocation*: the run pauses until a function response arrives. For incident response that is the wrong shape, because the investigation should finish and report while the remediation waits for an on-call human, possibly for hours. So:
- The agent's **only** write-path tool is `propose_remediation`. It validates parameters, computes a **dry-run preview** against current cluster state, assigns a **risk level**, and stores a `PendingAction` with a TTL.
- **Execution exists only** in `ApprovalService.decide(approve=True)`. It runs after a constant-time check of a **separate approver token** (not the API key) and a status/expiry check. Every step goes to `audit_log`.
- Tests prove it: wrong or missing token, rejection, expiry and double-approve never reach the writer. An AST test asserts that no agent or tool module references a `ClusterWriter` or the executor.
- RBAC mirrors this: a read-only ClusterRole for investigators, and a separate namespaced Role for the remediator.

## D7 - Sessions and resume
- `DatabaseSessionService` (SQLite + aiosqlite locally, any SQLAlchemy async URL otherwise) with `App(resumability_config=ResumabilityConfig(is_resumable=True))`.
- ADK persists every event and state delta as it is yielded, so **the event log is the checkpoint**. `after_agent_callback`s also append an explicit `{agent, invocation_id, at}` record per completed sub-agent step, so you can see which steps finished.
- Resume = `runner.run_async(invocation_id=<last>, new_message=None)`. `tests/unit/test_resume.py` crashes the supervisor right after LogAgent finishes, builds a brand-new service over the same DBs, resumes, and asserts that LogAgent's LLM was **not** called again.
- **Cloud Run store:** Cloud Run's filesystem is ephemeral, and the smallest Cloud SQL instance idles above the $5/month cap. In the order the brief preferred, the choice is the **Vertex AI Agent Engine session service** (`SESSION_DB_URL=vertexai://project/location/engine_id`). `scripts/deploy.sh` creates an Agent Engine used only as a session store, which has no idle compute. *Not exercised end to end yet, because GCP auth was unavailable during the build.* The SQLite path is what the tests prove.

## D8 - Observability
- We install the global OTel `TracerProvider`. ADK's native spans (`invoke_agent`, `execute_tool`, `generate_content`) and our plugin's spans land in one trace.
- `TelemetryPlugin` (an ADK `BasePlugin`) adds `agent:*`, `llm:*` and `tool:*` spans with `agent.name`, `llm.model`, `llm.input_tokens`, `llm.output_tokens`, `llm.tokens_per_sec`, `llm.cost_usd`, `tool.name` and `latency_ms`.
- `AgentTool` runs workers under different invocation IDs, so the usage ledger is a `ContextVar` scoped to the investigation, not keyed by invocation. Without that, worker cost would silently drop out of cost-per-request.
- Exporters: Cloud Trace (`TRACE_EXPORTER=gcp`), OTLP to Jaeger (docker-compose), console, none. Metrics go to Cloud Monitoring (`workload.googleapis.com/triage.*`), with an in-process rolling summary at `GET /metrics/summary`.
- Prices are in `telemetry/pricing.py`, with the check date, and can be overridden via `MODEL_PRICES_JSON`.

## D9 - RAG
Runbooks are chunked on `##` sections with front-matter metadata. Retrieval runs BM25 (`rank_bm25`) and cosine search over Gemini embeddings (`gemini-embedding-001`, 768-d), fuses them with Reciprocal Rank Fusion, then re-ranks: exact runbook symptom strings found in the query, a section prior (Symptoms/Diagnosis over Prevention), and at most 2 chunks per runbook. Every result carries `chunk_id` / `runbook_id` / `section` citations.
- The index is built with the API key **outside** the image (`scripts/deploy.sh`), then copied in and validated against a corpus hash at Docker build. No secret ever enters a layer. Without a key, the build creates a lexical index and `/readyz` reports `retrieval_mode=bm25`. BuildKit secret mounts were the first design, but they were dropped because neither the local Docker nor default Cloud Build guaranteed BuildKit.

## D10 - Evaluation and the rule-based baseline
- 8 fault scenarios. Each runs in its own `eval-<name>` namespace, starts from a healthy baseline app, then applies the fault and waits for the symptom.
- **Rule-based baseline (`LLM_BACKEND=baseline`):** a deterministic `BaseLlm` that plays every role by reading the real tool observations. It lets CI run the *whole* graph (tools, fixtures, RAG, HITL, scoring) on every PR without a key or cost. It was written knowing these fault types, so **its scores are a pipeline and regression check, not a measure of Gemini.** Every place that shows it (README, UI banner, `/readyz`, eval JSON) labels it.
- The gate is root-cause accuracy ≥ 0.75, a starting point to tighten once real Gemini numbers exist.

## D11 - GitHub workflow files
The `gh` token used during the build lacked the `workflow` scope, so GitHub rejected pushes touching `.github/workflows/`. The workflows were parked in `deploy/github-workflows/`. Local history was rewritten to relocate them **before the first push**; nothing that existed remotely was rewritten. On 2026-11-18, once the scope was granted, they were moved to `.github/workflows/`. `deploy.yml` became manual-only (`workflow_dispatch`), because the project runs offline by choice and Cloud Run is optional.

## D12 - Cost guardrails
- Cloud Run runs with min 0 / max 2 instances, 1 vCPU / 1 GiB, and concurrency 20.
- The API has a per-instance sliding-window rate limit of 20 investigations per hour, plus `max_llm_calls`, per-agent tool budgets and `max_critic_rounds`.
- No GKE and no Cloud SQL.
