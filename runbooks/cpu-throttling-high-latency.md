---
id: cpu-throttling-high-latency
title: CPU Throttling Causing High Latency
category: cpu_throttling
severity: sev3
symptoms: ["CPU throttling", "container_cpu_cfs_throttled_periods_total", "CPUThrottlingHigh", "high p99 latency", "timeouts", "context deadline exceeded", "Readiness probe failed", "cpu limit"]
---

## Summary

When a container has a CPU limit, the Linux CFS quota allows it at most `limit × cfs_period` (default 100ms period) of CPU time per period across all its threads. A multi-threaded app that burns through its quota early in a period is paused (throttled) until the next period, adding up to tens of milliseconds of latency per request even when average CPU usage looks low. Symptoms are elevated p95/p99 latency, timeouts, and slow health checks, with the pod `Running` and no restarts. This is a performance degradation, not a crash, unless probe timeouts cascade into restarts or unready pods.

## Symptoms

- Latency SLO alerts (p99 up, p50 normal or slightly up) with no errors in application logs.
- Prometheus alert `CPUThrottlingHigh`: `<x>% throttling of CPU in namespace <ns> for container <c> in pod <pod>.` (kube-prometheus default threshold 25%).
- Metric ratio `rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])` high (> 0.25).
- `kubectl top pod --containers` shows CPU usage at or near the limit (e.g. `998m` with `limits.cpu: 1`), or well below it yet still throttled (bursty threads).
- Probe events: `Readiness probe failed: ... context deadline exceeded (Client.Timeout exceeded while awaiting headers)`; liveness failures may cause restarts.
- Upstream callers log `504 Gateway Timeout`, `upstream request timeout`, `context deadline exceeded`.
- JVM/Go: long GC pauses; GC and JIT threads compete for the same quota. Go `GOMAXPROCS` defaults to node core count, not the limit.

## Likely Causes

- CPU limit set too low relative to peak (not average) demand.
- Runtime thread count sized to node cores (Go `GOMAXPROCS`, JVM `ActiveProcessorCount`, Node.js libuv pool, nginx workers) far exceeding the CPU limit, burning quota in bursts.
- Traffic increase without horizontal scaling (HPA missing or capped, see `hpa-not-scaling`).
- Noisy neighbor: node CPU saturated so even request-sized CPU is contended (no throttling metric, but high `node_cpu` usage and run-queue latency).
- New release with a more CPU-intensive code path (regex, serialization, TLS).
- Sidecar (service mesh proxy) with a tiny CPU limit throttling all traffic through the pod.

## Diagnosis

1. Check current usage vs requests/limits per container:
   `kubectl top pod <pod> -n <ns> --containers`
   `kubectl get pod <pod> -n <ns> -o jsonpath='{range .spec.containers[*]}{.name}{"\t"}{.resources.limits.cpu}{"\t"}{.resources.requests.cpu}{"\n"}{end}'`
2. Quantify throttling (PromQL):
   `sum by (pod, container) (rate(container_cpu_cfs_throttled_periods_total{namespace="<ns>"}[5m])) / sum by (pod, container) (rate(container_cpu_cfs_periods_total{namespace="<ns>"}[5m]))`
3. Read the kernel counters directly (cgroup v2):
   `kubectl exec -n <ns> <pod> -c <container> -- cat /sys/fs/cgroup/cpu.stat` — compare `nr_throttled` / `nr_periods` and `throttled_usec`; check `cat /sys/fs/cgroup/cpu.max` (e.g. `100000 100000` = 1 CPU).
4. Correlate latency spikes with throttling and request rate on dashboards.
5. Check node saturation (noisy neighbor):
   `kubectl top node` and `kubectl describe node <node> | grep -A8 "Allocated resources"`
6. Check runtime thread settings in the spec: `kubectl get deploy <deploy> -n <ns> -o yaml | grep -iE 'GOMAXPROCS|ActiveProcessorCount|WORKERS|UV_THREADPOOL'`
7. Check HPA status if it exists: `kubectl get hpa -n <ns>`.
8. Check for a recent rollout coinciding with the regression: `kubectl rollout history deployment/<deploy> -n <ns>`.

## Remediation

All remediation actions require human approval before execution.

- **Limit too low:** raise CPU limit (and request) — safe action `patch_resources`:
  `kubectl set resources deployment/<deploy> -n <ns> -c <container> --requests=cpu=1 --limits=cpu=2`
  Many teams remove CPU limits for latency-sensitive services and rely on accurate requests; follow cluster policy.
- **Load-driven:** add replicas immediately — safe action `scale_deployment`:
  `kubectl scale deployment/<deploy> -n <ns> --replicas=<n>` (if an HPA manages it, raise `minReplicas` instead, or the HPA will revert the change).
- **Regression in a new release:** `rollback_deployment`.
- **Thread oversubscription:** set `GOMAXPROCS` to the CPU limit (or use `automaxprocs`), JVM `-XX:ActiveProcessorCount`, and redeploy (`rollout_restart` after env change).
- **Noisy neighbor:** move the workload to less-loaded nodes (`rollout_restart` reschedules pods) or raise requests so the scheduler reserves real capacity.

## Prevention

- Set CPU requests at p95 usage; set limits generously (≥ 2× request) or omit them for latency-sensitive services.
- Align runtime parallelism with the container CPU allocation.
- Use HPA on CPU or request-rate metrics with sensible `maxReplicas`.
- Alert on throttling ratio > 25% sustained, and on p99 latency SLO burn.
- Load-test with production-like limits.

## Related

- [hpa-not-scaling](hpa-not-scaling.md)
- [readiness-probe-failure](readiness-probe-failure.md)
- [oomkilled](oomkilled.md)
- [pending-pods-insufficient-resources](pending-pods-insufficient-resources.md)
