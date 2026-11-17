---
id: oomkilled
title: Container OOMKilled (Out of Memory)
category: oom
severity: sev2
symptoms: ["OOMKilled", "Exit Code: 137", "Reason: OOMKilled", "memory limit", "CrashLoopBackOff", "Evicted", "The node was low on resource: memory"]
---

## Summary

A container is `OOMKilled` when its memory usage exceeds `resources.limits.memory` and the Linux kernel OOM killer terminates the process inside the container's cgroup with SIGKILL. The container exits with `Exit Code: 137` (128 + 9). Repeated OOM kills usually put the pod into `CrashLoopBackOff`. A related but distinct failure is node-pressure eviction, where the kubelet evicts pods because the node itself is low on memory (`Evicted`, `The node was low on resource: memory`).

## Symptoms

- `kubectl get pods`: `STATUS` = `OOMKilled` briefly, then `CrashLoopBackOff`, with `RESTARTS` increasing.
- `kubectl describe pod`:
  - `Last State: Terminated`
  - `Reason: OOMKilled`
  - `Exit Code: 137`
- Events: `Warning  BackOff  Back-off restarting failed container`.
- Node-level OOM (process killed by system OOM, not cgroup limit): node events `Warning  SystemOOM  System OOM encountered, victim process: <proc>`.
- Node-pressure eviction: pod `STATUS` = `Evicted`, `Message: The node was low on resource: memory. Container <name> was using <X>Ki, which exceeds its request of <Y>.`; node condition `MemoryPressure=True`.
- Application logs end abruptly with no shutdown message; JVM apps may show `java.lang.OutOfMemoryError` only if the heap limit is hit before the cgroup limit.

## Likely Causes

- Memory limit set too low for the real working set (common after traffic growth or a new feature).
- Memory leak: usage climbs steadily until the limit, then restarts at a regular interval.
- Runtime not container-aware: JVM `-Xmx` larger than the container limit, or heap + off-heap/metaspace/thread stacks exceeding it; Node.js `--max-old-space-size` unset; Go/Python caches unbounded.
- Large request payloads, batch jobs, or cache warm-ups causing spikes.
- Sidecar (proxy, log shipper) in the same pod using more memory than budgeted (each container has its own limit).
- Requests set far below limits (overcommit), so the node runs out of memory and evicts pods.

## Diagnosis

1. Confirm OOM is the termination reason:
   `kubectl get pod <pod> -n <ns> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.lastState.terminated.reason}{"\t"}{.lastState.terminated.exitCode}{"\n"}{end}'`
   Look for `OOMKilled  137`.
2. Check configured requests and limits per container:
   `kubectl get pod <pod> -n <ns> -o jsonpath='{range .spec.containers[*]}{.name}{"\t"}{.resources}{"\n"}{end}'`
3. Check current usage vs limit (requires metrics-server):
   `kubectl top pod <pod> -n <ns> --containers`
4. Look at restart timing to distinguish leak (regular interval) vs spike (correlated with traffic):
   `kubectl describe pod <pod> -n <ns>` — `Last State` `Started`/`Finished`, `Restart Count`.
5. Check for node-pressure evictions instead of cgroup OOM:
   `kubectl get pods -n <ns> --field-selector=status.phase=Failed` and `kubectl describe node <node> | grep -A5 Conditions` (look for `MemoryPressure True`).
6. Check node events for system OOM:
   `kubectl get events -A --field-selector reason=SystemOOM`
7. Inspect the runtime's memory flags in the spec (`JAVA_OPTS`, `-Xmx`, `-XX:MaxRAMPercentage`, `NODE_OPTIONS`):
   `kubectl get deploy <deploy> -n <ns> -o yaml | grep -iE 'xmx|MaxRAM|max-old-space'`
8. Correlate with recent deploys: `kubectl rollout history deployment/<deploy> -n <ns>`.

## Remediation

All remediation actions require human approval before execution.

- **Limit too low:** raise the memory limit (and request) — safe action `patch_resources`:
  `kubectl set resources deployment/<deploy> -n <ns> -c <container> --requests=memory=512Mi --limits=memory=1Gi`
  This triggers a new rollout. Size from observed peak usage plus 20–30% headroom.
- **Regression / leak introduced by a new release:** roll back — safe action `rollback_deployment`:
  `kubectl rollout undo deployment/<deploy> -n <ns>`
- **Leak with no fix yet:** a temporary `rollout_restart` resets memory across replicas; schedule a fix, this is not a remedy.
- **Load-driven spikes:** spread load over more replicas — safe action `scale_deployment`:
  `kubectl scale deployment/<deploy> -n <ns> --replicas=<n>`
- **JVM:** use `-XX:MaxRAMPercentage=75` instead of a fixed `-Xmx` exceeding the limit.
- **Node-pressure evictions:** set requests closer to real usage so the scheduler does not overpack nodes; consider `Guaranteed` QoS (requests == limits) for critical workloads.

## Prevention

- Set memory requests from p95 observed usage and limits with headroom; review with VPA in `Off`/recommendation mode.
- Alert on `container_memory_working_set_bytes / limit > 0.9` and on `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`.
- Load-test new releases for memory growth.
- Make runtimes container-aware (JVM 11+, explicit heap percentages).
- Use LimitRange defaults so containers never run without limits.

## Related

- [crashloopbackoff](crashloopbackoff.md)
- [pending-pods-insufficient-resources](pending-pods-insufficient-resources.md)
- [node-notready](node-notready.md)
- [cpu-throttling-high-latency](cpu-throttling-high-latency.md)
- [hpa-not-scaling](hpa-not-scaling.md)
