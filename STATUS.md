# Status: 2026-11-18

**Repo:** https://github.com/nikhil3495/k8s-triage-agents (public, `main`)
**Live URL:** **not deployed.** Hard limit hit (billing). At the start of the run `gcloud` had no credentialed account. By the end an account was logged in, but **neither visible project has billing enabled**: `sapient-office-450913-b7` is linked to a billing account but reports `billingEnabled: False`, and `red-parity-450913-r4` has no billing account. Cloud Run, Artifact Registry and Secret Manager require billing, and changing billing is outside what this run was allowed to do.
**Gemini:** no `GEMINI_API_KEY` was available, so no live Gemini call has been made. Everything Gemini-dependent is marked *pending* below and in the README.

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
| Cloud Trace | `CloudTraceSpanExporter` (`TRACE_EXPORTER=gcp`), set by `scripts/deploy.sh` | ⏳ wired, not yet exercised (no GCP auth); no Cloud Trace screenshot yet |
| Per-agent tokens/sec, cost per request, latency | span attributes `llm.tokens_per_sec`, `llm.cost_usd`, `latency_ms`; per-agent `usage` + `cost_usd` in the API response; `GET /metrics/summary`; `tests/unit/test_agent_flow.py` asserts cost > 0 and per-agent rollup | ✅ (real Gemini dollar figures pending) |
| Eval suite replays injected faults on a test cluster | `eval/` 8 scenarios, `eval/run_eval.py --mode live` ran on kind v1.33.1: **8/8** with the baseline model; fixtures recorded from kind (`eval/fixtures/`) | ✅ pipeline / ⏳ Gemini accuracy |
| Checks accuracy before each release | `deploy/github-workflows/eval.yml` (tag/release/dispatch, gate ≥ 0.75) | ⏳ workflows not yet enabled on GitHub (see below) |

## Eval numbers (real runs, 2026-11-17)
| Run | Model | Accuracy | Evidence recall | Runbook hit@3 | Critic agreement | p50 latency |
|---|---|---|---|---|---|---|
| live kind | rule-based baseline | 1.00 | 0.917 | 1.00 | 1.00 | 1.14 s |
| replay fixtures | rule-based baseline | 1.00 | 0.917 | 1.00 | 1.00 | 1.10 s |
| replay / live kind | Gemini | pending | pending | pending | pending | pending |

The baseline is deterministic rules that know these fault types. These rows validate the pipeline, not Gemini.

Quality: 83 tests passing, 95.7% coverage, `ruff` clean, `mypy --strict` clean, Docker image built and smoke-tested locally (non-root, `/healthz`, an `oom-killed` investigation completed).

## CI status
No GitHub Actions have run yet. The `gh` token has scopes `gist, read:org, repo, user` but **not `workflow`**, so GitHub rejects pushes to `.github/workflows/`. The three workflows are committed at `deploy/github-workflows/{ci,eval,deploy}.yml` (DECISIONS D11).

## Secrets file
`~/k8s-triage-agents-secrets.txt` **does not exist yet**. `scripts/deploy.sh` creates it (mode 600, outside the repo) with a generated `TRIAGE_API_KEY` and `APPROVER_TOKEN`, then appends the service URL and session store. No secrets exist in the repo; `scripts/secret_scan.py` reported 0 findings before each push.

## What's left: exact next commands
1. **Enable CI/eval/deploy workflows** (2 min):
   ```bash
   gh auth refresh -h github.com -s workflow
   cd ~/projects/k8s-triage-agents   # Windows: C:\Users\nikhil\projects\k8s-triage-agents
   mkdir -p .github && git mv deploy/github-workflows .github/workflows
   sed -i 's#deploy/github-workflows/#.github/workflows/#g' README.md STATUS.md docs/*.md
   git commit -am "ci: enable GitHub Actions workflows" && git push
   ```
2. **Gemini key** (get one at https://aistudio.google.com/apikey):
   ```bash
   export GEMINI_API_KEY=...
   gh secret set GEMINI_API_KEY --body "$GEMINI_API_KEY" --repo nikhil3495/k8s-triage-agents
   uv run python scripts/list_models.py          # confirm the default model ids exist
   uv run python -m eval.run_eval --mode replay --model gemini --out eval/results/replay-gemini.json
   ```
   Then replace the "pending" rows in README.md / docs/EVAL.md with `eval/results/replay-gemini.md`.
3. **Deploy to Cloud Run** (enable billing first, then one command):
   ```bash
   # check why billing is off (closed/suspended account?) in the console, or re-link it:
   #   gcloud billing projects link sapient-office-450913-b7 --billing-account=01B2C2-B610E1-474EDF
   gcloud config set project sapient-office-450913-b7
   GEMINI_API_KEY=... ./scripts/deploy.sh       # enables APIs, AR repo, secrets, SA, Agent Engine session store, Cloud Build, Cloud Run, smoke test
   ```
   Then put the URL in the README "Live demo" line and add a Cloud Trace screenshot (Trace explorer → filter `triage.investigation`).
4. **CI-based deploys (optional):** `./scripts/setup_wif.sh` (creates the WIF pool/provider/deployer SA and sets the repo variables). After that, every push to `main` deploys.
5. **Release gate on real kind + Gemini:** `git tag v0.1.0 && git push --tags` (runs `eval.yml`), or `gh workflow run eval.yml`.

## Local leftovers from the build
- The kind cluster and Jaeger container were deleted at the end of the run. The image `k8s-triage-agents:local` remains in WSL Docker (`wsl -e docker rmi k8s-triage-agents:local`).
- Installed into WSL `~/bin`: `kind` v0.29.0, `kubectl` v1.37.2; buildx was **not** installed (download timed out). Recreate the eval cluster with `wsl -e ~/bin/kind create cluster --name triage-eval`.
