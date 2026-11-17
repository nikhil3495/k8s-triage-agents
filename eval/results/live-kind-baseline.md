# Eval report - live / baseline

Run at 2026-11-17T18:31:09.988767+00:00 · retrieval `bm25` · gate: root-cause accuracy >= 0.75 -> **PASS**

| Metric | Value |
|---|---|
| scenarios | 8 |
| root_cause_accuracy | 1.0 |
| evidence_recall | 0.917 |
| runbook_hit_at_3 | 1.0 |
| critic_agreement | 1.0 |
| latency_ms_p50 | 1143.2 |
| cost_usd_mean | 0.0 |
| cost_usd_total | 0.0 |

| Scenario | Predicted | Expected | Evidence recall | Runbook hit@3 | Critic | Latency ms | Cost $ |
|---|---|---|---|---|---|---|---|
| bad-image-tag | image_pull | image_pull | 1.0 | True | PASS | 2835 | 0.00000 |
| broken-service-selector | service_selector | service_selector | 1.0 | True | PASS | 2215 | 0.00000 |
| cpu-throttling | cpu_throttling | cpu_throttling | 1.0 | True | PASS | 1074 | 0.00000 |
| crashloop-command | crashloop | crashloop | 1.0 | True | PASS | 907 | 0.00000 |
| failing-readiness | readiness_probe | readiness_probe | 0.667 | True | PASS | 1251 | 0.00000 |
| missing-configmap | missing_config | missing_config | 1.0 | True | PASS | 957 | 0.00000 |
| oom-killed | oom | oom | 1.0 | True | PASS | 1132 | 0.00000 |
| unschedulable | unschedulable | unschedulable | 0.667 | True | PASS | 1154 | 0.00000 |
