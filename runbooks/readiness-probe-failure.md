---
id: readiness-probe-failure
title: Readiness Probe Failing (Pods Running but Not Ready)
category: readiness_probe
severity: sev2
symptoms: ["Readiness probe failed", "Unhealthy", "0/1 Running", "Ready: False", "ContainersNotReady", "connection refused", "HTTP probe failed with statuscode: 503", "context deadline exceeded"]
---

## Summary

A readiness probe tells Kubernetes whether a container can accept traffic. When it fails, the container keeps running (it is not restarted, unlike a liveness probe failure) but the pod is marked `Ready: False` and removed from every Service's endpoints. If all replicas fail readiness, the Service has no endpoints and clients receive connection errors or 503s from the ingress. During a rollout, failing readiness blocks progress because new pods never become available.

## Symptoms

- `kubectl get pods`: `STATUS` = `Running` but `READY` = `0/1` (or `1/2` for multi-container pods), `RESTARTS` usually `0`.
- `kubectl describe pod`:
  - `Ready: False` on the container
  - Conditions: `Ready False`, `ContainersReady False`
  - Events: `Warning  Unhealthy  kubelet  Readiness probe failed: HTTP probe failed with statuscode: 503`
  - or `Readiness probe failed: Get "http://10.2.3.4:8080/healthz": dial tcp 10.2.3.4:8080: connect: connection refused`
  - or `Readiness probe failed: Get "...": context deadline exceeded (Client.Timeout exceeded while awaiting headers)`
  - or `Readiness probe failed: command "..." timed out` / exec probe non-zero exit
- `kubectl get endpoints <svc>` shows no addresses, or addresses only under `notReadyAddresses`.
- `kubectl rollout status` hangs: `Waiting for deployment "x" rollout to finish: 1 of 3 updated replicas are available...`
- Ingress/LB returns `503 Service Temporarily Unavailable` or `no healthy upstream`.

## Likely Causes

- Probe misconfiguration: wrong `port`, `path`, or `scheme` (HTTP vs HTTPS), or the app listens on `127.0.0.1` instead of `0.0.0.0`.
- `timeoutSeconds` (default 1s) too short for the health endpoint under load.
- `initialDelaySeconds` too short and no `startupProbe`, so slow-starting apps fail early (readiness will recover, but the rollout is slow).
- Health endpoint checks downstream dependencies (DB, cache) and one is down, making all replicas unready simultaneously (cascading outage).
- CPU throttling makes the health endpoint slow (see `cpu-throttling-high-latency`).
- Application genuinely unhealthy: thread pool exhausted, warm-up not complete, deadlock.
- NetworkPolicy blocking kubelet-to-pod traffic (rare; the kubelet probes from the node).

## Diagnosis

1. List not-ready pods:
   `kubectl get pods -n <ns> -o wide` — find `Running` pods with `READY 0/1`.
2. Read probe failure messages:
   `kubectl describe pod <pod> -n <ns>` — `Events` with `Reason: Unhealthy`; note statuscode, `connection refused`, or `context deadline exceeded`.
3. Print the probe definition:
   `kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.containers[*].readinessProbe}'`
   Compare `port`, `path`, `timeoutSeconds`, `periodSeconds`, `failureThreshold` with the app's actual listener.
4. Hit the endpoint from inside the pod:
   `kubectl exec -n <ns> <pod> -- wget -qO- -T 2 http://localhost:<port><path>` (or `curl -sv`).
   Fast success locally but probe timeouts suggest CPU starvation or binding to localhost only.
5. Check which ports the app listens on:
   `kubectl exec -n <ns> <pod> -- netstat -tlnp` (or `ss -tlnp`).
6. Check endpoint membership:
   `kubectl get endpointslices -n <ns> -l kubernetes.io/service-name=<svc> -o yaml` — `conditions.ready: false`.
7. Read app logs for dependency errors:
   `kubectl logs <pod> -n <ns> --tail=200`
8. Check CPU throttling / usage: `kubectl top pod <pod> -n <ns> --containers`.

## Remediation

All remediation actions require human approval before execution.

- **New release broke the health endpoint or app:** roll back — safe action `rollback_deployment`:
  `kubectl rollout undo deployment/<deploy> -n <ns>`.
- **Probe misconfigured:** correct port/path/scheme or raise `timeoutSeconds` / `failureThreshold`; add a `startupProbe` for slow boots. Apply via the Deployment manifest (triggers a rollout).
- **Dependency outage making all replicas unready:** restore the dependency; then, if pods do not recover on their own, `rollout_restart`. Longer term, make readiness check only local health.
- **Health endpoint slow due to CPU throttling:** raise CPU limits/requests — safe action `patch_resources`.
- **Load-induced:** add replicas — safe action `scale_deployment`.
- Do not "fix" by removing the readiness probe; that sends traffic to broken pods.

## Prevention

- Keep readiness endpoints cheap, local, and independent of shared downstreams; use liveness only for deadlock detection.
- Use `startupProbe` for apps with variable boot times.
- Set `timeoutSeconds` >= 2–5s for HTTP probes on loaded services.
- Alert on `kube_pod_status_ready{condition="false"}` and on Services with zero ready endpoints.
- Use `maxUnavailable: 0` in rolling updates so unready new pods do not reduce capacity.

## Related

- [service-no-endpoints](service-no-endpoints.md)
- [failing-rollout](failing-rollout.md)
- [cpu-throttling-high-latency](cpu-throttling-high-latency.md)
- [crashloopbackoff](crashloopbackoff.md)
- [dns-failure](dns-failure.md)
