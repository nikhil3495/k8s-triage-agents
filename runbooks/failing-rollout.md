---
id: failing-rollout
title: Deployment Rollout Stuck or Failing
category: rollout
severity: sev2
symptoms: ["ProgressDeadlineExceeded", "Progressing False", "Waiting for deployment rollout to finish", "error: deployment exceeded its progress deadline", "ReplicaSetCreateError", "FailedCreate", "exceeded quota", "UNAVAILABLE", "old replicas are pending termination"]
---

## Summary

A Deployment rollout creates a new ReplicaSet and shifts replicas from the old one according to `strategy.rollingUpdate.maxSurge` and `maxUnavailable`. It progresses only as new pods become Ready (and stay Ready for `minReadySeconds`). If no progress is made within `progressDeadlineSeconds` (default 600s), the Deployment condition becomes `Progressing=False, Reason=ProgressDeadlineExceeded`. Kubernetes does **not** roll back automatically; the old ReplicaSet keeps serving with reduced capacity while new pods fail. The root cause lies in the new pods (crash, image pull, probe, config, scheduling) or in pod creation itself (quota, admission webhook).

## Symptoms

- `kubectl rollout status deployment/<deploy>`:
  - `Waiting for deployment "<deploy>" rollout to finish: 1 out of 3 new replicas have been updated...`
  - `Waiting for deployment "<deploy>" rollout to finish: 1 old replicas are pending termination...`
  - `error: deployment "<deploy>" exceeded its progress deadline`
- `kubectl get deploy`: `UP-TO-DATE` < `REPLICAS`, `AVAILABLE` < desired.
- `kubectl describe deploy` conditions:
  - `Progressing  False  ProgressDeadlineExceeded`
  - `Available  False  MinimumReplicasUnavailable`
  - `ReplicaFailure  True  FailedCreate`
- Two ReplicaSets with pods: `kubectl get rs` shows the new RS with `READY 0` and the old RS still running.
- New pods in `CrashLoopBackOff`, `ImagePullBackOff`, `CreateContainerConfigError`, `Pending`, or `Running 0/1`.
- ReplicaSet events: `Warning  FailedCreate  Error creating: pods "x-..." is forbidden: exceeded quota: compute-quota, requested: limits.cpu=500m, used: limits.cpu=8, limited: limits.cpu=8`
- `Error creating: admission webhook "<name>" denied the request: ...`

## Likely Causes

- New image crashes or fails readiness (application regression).
- Image tag not found / registry auth failure.
- New ConfigMap/Secret reference missing.
- Increased resource requests do not fit the cluster (`Pending`) or exceed a namespace ResourceQuota (`FailedCreate`).
- Admission policy (OPA Gatekeeper, Kyverno, Pod Security Admission) rejects the new pod spec: `violates PodSecurity "restricted:latest"`.
- PodDisruptionBudget plus `maxUnavailable: 0` and no surge capacity.
- `progressDeadlineSeconds` shorter than the app's legitimate start time.
- RWO PVC shared by old and new pods under `RollingUpdate` (Multi-Attach error).

## Diagnosis

1. Check rollout state:
   `kubectl rollout status deployment/<deploy> -n <ns> --timeout=10s`
2. Read Deployment conditions:
   `kubectl describe deployment <deploy> -n <ns>` — `Conditions`, `NewReplicaSet`, `OldReplicaSets`, `Events`.
3. Compare ReplicaSets and their images:
   `kubectl get rs -n <ns> -l app=<label> -o wide`
4. Check ReplicaSet creation errors (quota, webhook):
   `kubectl describe rs <new-rs> -n <ns>` — look for `FailedCreate`.
5. Inspect new pods and route to the matching runbook:
   `kubectl get pods -n <ns> -l pod-template-hash=<hash>` then `kubectl describe pod <pod> -n <ns>` and `kubectl logs <pod> -n <ns> --previous`.
6. Diff the new vs previous revision:
   `kubectl rollout history deployment/<deploy> -n <ns>` and `kubectl rollout history deployment/<deploy> -n <ns> --revision=<n>`
7. Check quota usage: `kubectl describe resourcequota -n <ns>`.
8. Check strategy settings: `kubectl get deploy <deploy> -n <ns> -o jsonpath='{.spec.strategy}{"\n"}{.spec.progressDeadlineSeconds}{"\n"}{.spec.minReadySeconds}'`

## Remediation

All remediation actions require human approval before execution.

- **Default for a bad release:** roll back to the previous revision — safe action `rollback_deployment`:
  `kubectl rollout undo deployment/<deploy> -n <ns>` (or `--to-revision=<n>`), then confirm with `kubectl rollout status`.
- **Quota exceeded:** lower requests/limits — `patch_resources` — or have the quota raised; then re-run the rollout.
- **Pending due to capacity:** add nodes or temporarily `scale_deployment` non-critical workloads down; see `pending-pods-insufficient-resources`.
- **Transient failure (dependency now fixed):** `kubectl rollout restart deployment/<deploy> -n <ns>` (`rollout_restart`) to create fresh pods and restart progress.
- **Webhook/policy rejection:** fix the pod spec to comply (securityContext, labels) rather than disabling the policy.
- **Slow-starting app:** raise `progressDeadlineSeconds` and add a `startupProbe`.
- Use `kubectl rollout pause deployment/<deploy>` to freeze a partially rolled-out Deployment while investigating.

## Prevention

- Use progressive delivery (Argo Rollouts / Flagger canary with automated analysis and abort).
- Set `maxUnavailable: 0`, `maxSurge: 25%` so failing new pods never reduce serving capacity.
- Validate manifests with `kubectl apply --dry-run=server` in CI to catch quota/admission rejections early.
- Alert on `kube_deployment_status_condition{condition="Progressing",status="false"}` and on `kube_deployment_status_replicas_unavailable > 0` for > 10 minutes.
- Keep `revisionHistoryLimit` ≥ 5 so rollback targets exist.

## Related

- [crashloopbackoff](crashloopbackoff.md)
- [imagepullbackoff](imagepullbackoff.md)
- [readiness-probe-failure](readiness-probe-failure.md)
- [missing-configmap-secret](missing-configmap-secret.md)
- [pending-pods-insufficient-resources](pending-pods-insufficient-resources.md)
- [pvc-binding](pvc-binding.md)
