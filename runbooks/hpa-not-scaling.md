---
id: hpa-not-scaling
title: HorizontalPodAutoscaler Not Scaling
category: hpa
severity: sev2
symptoms: ["<unknown>/80%", "FailedGetResourceMetric", "FailedComputeMetricsReplicas", "unable to get metrics for resource cpu", "missing request for cpu", "ScalingLimited", "TooManyReplicas", "AbleToScale False", "ScalingActive False"]
---

## Summary

A HorizontalPodAutoscaler (`autoscaling/v2`) periodically (every 15s) reads metrics for its target, computes `desiredReplicas = ceil(currentReplicas × currentMetric / targetMetric)`, and updates the target's scale subresource within `minReplicas`/`maxReplicas`. It fails to scale when metrics are unavailable (metrics-server or adapter down, missing resource requests), when it is capped at `maxReplicas`, when stabilization windows or scaling policies hold it back, or when new replicas are created but stay `Pending`. The HPA's conditions (`AbleToScale`, `ScalingActive`, `ScalingLimited`) explain which.

## Symptoms

- `kubectl get hpa`: `TARGETS` = `<unknown>/80%` (metrics unavailable) or `cpu: 250%/80%` with `REPLICAS` equal to `MAXPODS`.
- `kubectl describe hpa` conditions/events:
  - `ScalingActive  False  FailedGetResourceMetric  the HPA was unable to compute the replica count: failed to get cpu utilization: unable to get metrics for resource cpu: unable to fetch metrics from resource metrics API: the server could not find the requested resource (get pods.metrics.k8s.io)`
  - `failed to get cpu utilization: missing request for cpu in container <c> of Pod <pod>`
  - `Warning  FailedGetExternalMetric` / `FailedGetPodsMetric  unable to get metric http_requests_per_second: no metrics returned from custom metrics API`
  - `ScalingLimited  True  TooManyReplicas  the desired replica count is more than the maximum replica count`
  - `AbleToScale  True  ScaleDownStabilized  recent recommendations were higher than current one, applying the highest recent recommendation`
  - `AbleToScale  False  FailedGetScale  ... deployments/scale.apps "x" not found`
- `kubectl top pods` → `error: Metrics API not available`.
- Latency/CPU high while replica count stays flat; or replicas increase but new pods are `Pending`.

## Likely Causes

- metrics-server not installed, crashing, or failing to scrape kubelets (`x509: cannot validate certificate ... because it doesn't contain any IP SANs`).
- Target containers have no `resources.requests.cpu`/`memory` — utilization targets require requests on every container (including sidecars).
- Prometheus Adapter / KEDA misconfigured, so custom/external metrics return nothing.
- `maxReplicas` reached.
- `behavior.scaleUp` policies or `stabilizationWindowSeconds` too conservative.
- `scaleTargetRef` points to a wrong name or kind.
- Manual `kubectl scale` or a GitOps tool fighting the HPA by setting `spec.replicas`.
- Utilization never crosses target because the bottleneck is elsewhere (CPU throttled at limit with low requests looks like >100%; or the app is I/O-bound so CPU stays low).
- Scaled pods unschedulable (cluster capacity).

## Diagnosis

1. Inspect HPA state:
   `kubectl get hpa -n <ns>` and `kubectl describe hpa <hpa> -n <ns>` — read `Conditions` and `Events`.
2. Check the resource metrics API:
   `kubectl get apiservice v1beta1.metrics.k8s.io` (look for `AVAILABLE True`) and `kubectl top pods -n <ns>`.
3. Check metrics-server health:
   `kubectl get pods -n kube-system -l k8s-app=metrics-server` and `kubectl logs -n kube-system deploy/metrics-server --tail=100`
4. Verify requests exist on every container in the target:
   `kubectl get deploy <deploy> -n <ns> -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\t"}{.resources.requests}{"\n"}{end}'`
5. For custom metrics:
   `kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta2/namespaces/<ns>/pods/*/<metric>"` and `kubectl get apiservice | grep -E 'custom|external'`
6. Check the HPA spec (min/max, behavior, scaleTargetRef):
   `kubectl get hpa <hpa> -n <ns> -o yaml`
7. Check for new pods stuck Pending: `kubectl get pods -n <ns> --field-selector=status.phase=Pending`.
8. Check whether something else sets replicas (Argo CD/Flux diff on `spec.replicas`): `kubectl get deploy <deploy> -n <ns> -o yaml | grep -A3 managedFields` or `--show-managed-fields`.

## Remediation

All remediation actions require human approval before execution.

- **Immediate capacity during an incident:** raise `minReplicas` (preferred, the HPA respects it):
  `kubectl patch hpa <hpa> -n <ns> -p '{"spec":{"minReplicas":<n>}}'`
  Direct `scale_deployment` works only briefly — the HPA will override `spec.replicas` on its next sync unless metrics are unavailable.
- **At maxReplicas:** raise `maxReplicas` after confirming cluster capacity.
- **Missing requests:** add CPU/memory requests to all containers — safe action `patch_resources` (triggers a rollout).
- **metrics-server down:** `rollout_restart` the metrics-server Deployment; fix kubelet TLS flags (`--kubelet-preferred-address-types`, proper serving certs rather than `--kubelet-insecure-tls` in production).
- **Custom metrics adapter broken:** fix the adapter rules/query and restart it.
- **Pods Pending after scale-up:** see `pending-pods-insufficient-resources`.
- **Remove `spec.replicas` from GitOps manifests** for HPA-managed Deployments.

## Prevention

- Set requests on every container (LimitRange defaults) for HPA-targeted workloads.
- Run metrics-server with 2 replicas and a PDB; alert on `APIService v1beta1.metrics.k8s.io` unavailability.
- Alert on `kube_horizontalpodautoscaler_status_condition{condition="ScalingLimited",status="true"}` and on HPAs at max for > 15 minutes.
- Tune `behavior.scaleUp` for fast reaction (e.g. 100% per 15s) and longer scale-down stabilization.
- Load-test autoscaling paths.

## Related

- [cpu-throttling-high-latency](cpu-throttling-high-latency.md)
- [pending-pods-insufficient-resources](pending-pods-insufficient-resources.md)
- [oomkilled](oomkilled.md)
- [failing-rollout](failing-rollout.md)
