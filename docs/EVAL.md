# Evaluation suite

## Scenarios (`eval/scenarios/*.yaml`)
| Scenario | Injected fault | Expected category | Expected runbook |
|---|---|---|---|
| bad-image-tag | image tag `nginx:1.27.99-doesnotexist` | image_pull | imagepullbackoff |
| oom-killed | `tail /dev/zero` under a 48Mi memory limit | oom | oomkilled |
| crashloop-command | command exits 1 (missing config file) | crashloop | crashloopbackoff |
| failing-readiness | readiness probe on a 404 path | readiness_probe | readiness-probe-failure |
| missing-configmap | `envFrom` an absent ConfigMap | missing_config | missing-configmap-secret |
| unschedulable | requests `cpu: 64` | unschedulable | pending-pods-insufficient-resources |
| broken-service-selector | Service selector `app: checkout-v2` | service_selector | service-no-endpoints |
| cpu-throttling | busy loop under a 50m CPU limit | cpu_throttling | cpu-throttling-high-latency |

Each scenario gets its own `eval-<name>` namespace. The harness applies the healthy base app, waits for rollout, applies the fault, then polls until the symptom is observable (pod reason / event / ready deployment / metrics), plus a 10 s settle.

## Metrics (`eval/scoring.py`)
- **Root-cause accuracy:** `hypothesis.category == expected.category`. This is the release-gate metric.
- **Evidence recall:** fraction of the scenario's required evidence keywords found in the hypothesis (root cause, summary and evidence snippets).
- **Runbook hit@3:** whether the expected runbook is in the top 3 `runbook_refs` (falls back to retrieval citations).
- **Critic agreement:** whether the critic's final verdict matches correctness (PASS on a right answer, REVISE on a wrong one).
- **Latency and cost:** end to end, from the telemetry ledger.

## Modes
```bash
# every PR (CI): recorded fixtures + rule-based baseline, no key, ~10 s
uv run python -m eval.run_eval --mode replay --model baseline
# recorded fixtures + Gemini
GEMINI_API_KEY=... uv run python -m eval.run_eval --mode replay --model gemini
# release gate: kind, inject, investigate live, refresh fixtures
GEMINI_API_KEY=... uv run python -m eval.run_eval --mode live --model gemini --record
```
The gate fails the process (and the `eval` workflow) when root-cause accuracy is below `--threshold` (default **0.75**).

## Results so far (real runs, 2026-11-17)
| Run | Model | Cluster | Accuracy | Evidence recall | Runbook hit@3 | Critic agreement | p50 latency | Cost |
|---|---|---|---|---|---|---|---|---|
| `eval/results/live-kind-baseline.*` | rule-based baseline | **live kind v1.33.1** | 1.00 (8/8) | 0.917 | 1.00 | 1.00 | 1.14 s | $0 |
| `eval/results/offline-baseline.*` | rule-based baseline | replay fixtures | 1.00 (8/8) | 0.917 | 1.00 | 1.00 | 1.10 s | $0 |
| Gemini, replay | gemini-3.8-flash / 3.1-flash-lite | replay fixtures | **pending** | pending | pending | pending | pending | pending |
| Gemini, live kind | gemini-3.8-flash / 3.1-flash-lite | live kind | **pending** | pending | pending | pending | pending | pending |

**How to read the baseline rows.** The baseline is deterministic rules written by someone who knew these 8 fault types. A perfect score shows that the *pipeline* works end to end: fault injection on kind, live API reads, fixture fidelity, every tool, retrieval hit@3, the HITL proposal path, scoring and the gate. It says nothing about Gemini's diagnostic ability. The Gemini rows stay "pending" until a run with a `GEMINI_API_KEY` happens. They were not run during the build because no key was available.

Lowest baseline evidence recall: `failing-readiness` and `unschedulable` (0.667), where one expected keyword (`/healthz`, `Pending`) is missing from the extracted snippets.
