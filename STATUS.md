# Status: 2026-11-18

**Repo:** https://github.com/nikhil3495/k8s-triage-agents (public, `main`)
**Deployment:** **Not deployed by choice; runs fully offline.** There is no hosted URL. Run it locally with `docker compose up --build` (API + UI + Jaeger) or `uv run`; see README "Run locally". Cloud Run (`scripts/deploy.sh`) and Cloud Trace are **deploy-ready, not live**. No GCP resource was created.
**Gemini:** no `GEMINI_API_KEY` was available, so no live Gemini call has been made. Everything Gemini-dependent is marked *pending*. The offline `LLM_BACKEND=baseline` mode runs the whole graph without a key.
**CI:** GitHub Actions is enabled (`.github/workflows/`). The first `ci` run on `main` was green (test, security, docker) on 2026-11-18. `deploy.yml` is manual-only.

## Resume claims → proof

### Bullet 1: hierarchical multi-agent system on ADK + Gemini
| Claim | Proof | Status |
|---|---|---|
| Built with Google ADK | `google-adk==2.14.1` in `uv.lock`; `src/triage/agents/factory.py` | ✅ |
| Gemini models | `SUPERVISOR_MODEL=gemini-3.8-flash`, `WORKER_MODEL=gemini-3.1-flash-lite`, `CRITIC_MODEL=gemini-3.8-flash` (`config.py`, DECISIONS D4) | ✅ wired / ⏳ not yet called (no key) |
| Supervisor delegates to log, metrics, runbook sub-agents | `AgentTool(LogAgent/MetricsAgent/RunbookAgent)`; `tests/unit/test_agent_flow.py::test_full_hierarchy_pass` asserts all five agents ran | ✅ |
| Each runs a ReAct tool-calling loop against the Kubernetes API | `PlanReActPlanner` + `tools/cluster_tools.py` → `k8s/live.py`; tool budget `sessions/checkpoints.py`; **live run against kind:** `eval/results/live-kind-baseline.*` | ✅ |
| Self-reflection critic verifies before reporting | Critic + `VerdictGate` in `factory.py`; `test_critic_revise_triggers_second_round` (REVISE → second round → PASS); verdicts stored in `state.critic_history` | ✅ |
| RAG over runbooks with citations | 14 runbooks, `rag/retriever.py` (BM25 + embeddings, RRF, re-rank); `tests/unit/test_rag.py` (13 queries, hit@3) | ✅ BM25 path tested / ⏳ embeddings need key |

### Bullet 2: state management and safety
| Claim | Proof | Status |
|---|---|---|
| Persistent sessions | ADK `DatabaseSessionService` (`sessions/service.py`), `SESSION_DB_URL` | ✅ |
| Investigations resume after failure | `tests/unit/test_resume.py::test_crash_then_resume_continues_from_checkpoint` (crash mid-run, new process objects, resume, LogAgent not re-run); `POST /investigations/{id}/resume` | ✅ |
| Every remediation gated by human approval | `hitl/service.py`; `tests/unit/test_hitl.py` (bad/missing token, reject, expiry, double-approve, AST scan: no writer reachable from agent code); `tests/unit/test_api.py::test_end_to_end_with_approval_gate`; screenshot `docs/img/ui-investigation.png` | ✅ |
| Dry-run preview, risk, audit log | `hitl/models.py::build_preview/assess_risk`; `audit_log` table; `GET /approvals/{id}/audit` | ✅ |

### Bullet 3: observability and evaluation
| Claim | Proof | Status |
|---|---|---|
| OpenTelemetry tracing | `telemetry/setup.py`, `telemetry/plugin.py`; 85-span trace in `docs/img/trace-waterfall-jaeger.png` | ✅ |
| Cloud Trace | `CloudTraceSpanExporter` (`TRACE_EXPORTER=gcp`), set by `scripts/deploy.sh` | 🟡 deploy-ready, not live (same spans proven via OTLP → Jaeger) |
| Cloud Run | `scripts/deploy.sh`, `Dockerfile` (built and smoke-tested locally), manual `deploy.yml` | 🟡 deploy-ready, not live (not deployed by choice) |
| Per-agent tokens/sec, cost per request, latency | span attributes `llm.tokens_per_sec`, `llm.cost_usd`, `latency_ms`; per-agent `usage` + `cost_usd` in the API response; `GET /metrics/summary`; `tests/unit/test_agent_flow.py` asserts cost > 0 and per-agent rollup | ✅ (real Gemini dollar figures pending) |
| Eval suite replays injected faults on a test cluster | `eval/` 8 scenarios, `eval/run_eval.py --mode live` ran on kind v1.33.1: **8/8** with the baseline model; fixtures recorded from kind (`eval/fixtures/`) | ✅ pipeline / ⏳ Gemini accuracy |
| Checks accuracy before each release | `.github/workflows/eval.yml` (tag/release/dispatch, gate ≥ 0.75); `ci.yml` runs the offline eval on every push | ✅ enabled; gate run on GitHub Actions with the baseline: [PASS 8/8](https://github.com/nikhil3495/k8s-triage-agents/actions/runs/44169113577) / ⏳ Gemini gate needs key |

## Eval numbers (real runs, 2026-11-17/18)
| Run | Model | Accuracy | Evidence recall | Runbook hit@3 | Critic agreement | p50 latency |
|---|---|---|---|---|---|---|
| live kind | rule-based baseline | 1.00 | 0.917 | 1.00 | 1.00 | 1.14 s |
| [CI kind (GitHub Actions)](https://github.com/nikhil3495/k8s-triage-agents/actions/runs/44169113577), 2026-11-18, gate PASS | rule-based baseline | 1.00 | 0.917 | 1.00 | 1.00 | 0.17 s |
| replay fixtures | rule-based baseline | 1.00 | 0.917 | 1.00 | 1.00 | 1.10 s |
| replay / live kind | Gemini | pending | pending | pending | pending | pending |

The baseline is deterministic rules that know these fault types. These rows validate the pipeline, not Gemini.

Quality: 83 tests passing, 95.7% coverage, `ruff` clean, `mypy --strict` clean, Docker image built and smoke-tested locally (non-root, `/healthz`, an `oom-killed` investigation completed).

## CI status
The `gh` token now has the `workflow` scope, so the workflows moved from `deploy/github-workflows/` to `.github/workflows/` (DECISIONS D11).
- `ci.yml` (push/PR): ruff, mypy, pytest + coverage, offline eval, gitleaks, pip-audit, Docker build. **Green** on `main`.
- `eval.yml` (tags/releases/dispatch): kind release gate. Ran with `model=baseline` on 2026-11-18: **PASS**, 8/8, in 4m06s (https://github.com/nikhil3495/k8s-triage-agents/actions/runs/44169113577). With `model=gemini` (the default for tags) it needs the `GEMINI_API_KEY` repo secret.
- `deploy.yml`: `workflow_dispatch` only, optional, and skips itself unless `GCP_PROJECT` is set.

## Secrets file
`~/k8s-triage-agents-secrets.txt` does not exist; it is only created by `scripts/deploy.sh`, which has not been run. No secrets exist in the repo: `scripts/secret_scan.py` reported 0 findings before each push, and gitleaks runs in CI.

## Optional next steps
1. **Gemini numbers** (key from https://aistudio.google.com/apikey):
   ```bash
   export GEMINI_API_KEY=...
   uv run python scripts/list_models.py          # confirm the default model ids exist
   uv run python -m eval.run_eval --mode replay --model gemini --out eval/results/replay-gemini.json
   gh secret set GEMINI_API_KEY --repo nikhil3495/k8s-triage-agents   # enables the Gemini release gate
   ```
   Then replace the "pending" rows in README.md / docs/EVAL.md with `eval/results/replay-gemini.md`.
2. **Release gate:** `git tag v0.1.0 && git push --tags` (runs `eval.yml` on kind).
3. **Cloud Run (optional, needs a billing-enabled project):** `gcloud config set project <id> && GEMINI_API_KEY=... ./scripts/deploy.sh`. For CI deploys, run `./scripts/setup_wif.sh`, then trigger `deploy.yml` manually.

## Local leftovers from the build
- The kind cluster and Jaeger container were deleted at the end of the run. The image `k8s-triage-agents:local` remains in WSL Docker (`wsl -e docker rmi k8s-triage-agents:local`).
- Installed into WSL `~/bin`: `kind` v0.29.0, `kubectl` v1.37.2; buildx was **not** installed (download timed out). Recreate the eval cluster with `wsl -e ~/bin/kind create cluster --name triage-eval`.
