---
id: missing-configmap-secret
title: Missing ConfigMap or Secret (CreateContainerConfigError)
category: missing_config
severity: sev2
symptoms: ["CreateContainerConfigError", "configmap not found", "secret not found", "couldn't find key", "FailedMount", "MountVolume.SetUp failed", "ContainerCreating"]
---

## Summary

When a pod references a ConfigMap or Secret that does not exist (or a key that does not exist in it), the container cannot be created. References via `env.valueFrom` or `envFrom` fail fast with `CreateContainerConfigError`. References via a `volume` keep the pod in `ContainerCreating` with `FailedMount` events. The kubelet retries, so the pod recovers automatically once the object is created — unless the reference is marked `optional: true`, in which case it starts without the value and may misbehave instead.

## Symptoms

- `kubectl get pods`: `STATUS` = `CreateContainerConfigError` (env reference) or `ContainerCreating` (volume reference), `READY 0/1`.
- `kubectl describe pod`:
  - `State: Waiting`, `Reason: CreateContainerConfigError`
  - Events: `Warning  Failed  kubelet  Error: configmap "app-config" not found`
  - `Error: secret "db-credentials" not found`
  - `Error: couldn't find key DATABASE_URL in ConfigMap prod/app-config`
  - `Error: couldn't find key password in Secret prod/db-credentials`
  - Volume: `Warning  FailedMount  MountVolume.SetUp failed for volume "config" : configmap "app-config" not found`
  - `MountVolume.SetUp failed for volume "tls" : secret "api-tls" not found`
- Projected / service account token issues: `MountVolume.SetUp failed for volume "kube-api-access-xxxxx"`.

## Likely Causes

- Deployment applied before its ConfigMap/Secret (ordering in CI/CD, Helm hook, or Kustomize overlay missing the resource).
- Object exists in a different namespace (ConfigMaps/Secrets are namespace-scoped and cannot be referenced cross-namespace).
- Typo in name or key, or a key renamed in a config change.
- Kustomize `configMapGenerator` hash suffix (`app-config-7g2k9m`) not propagated because the reference is outside a managed field.
- External Secrets Operator / Sealed Secrets / CSI secrets store failed to sync, so the Secret was never created (`SecretSyncedError`).
- Secret deleted by cleanup automation or a `helm uninstall` of another release that owned it.
- Secret for cert-manager not yet issued (see `cert-expiry`).

## Diagnosis

1. Find affected pods:
   `kubectl get pods -n <ns> | grep -E 'CreateContainerConfigError|ContainerCreating'`
2. Read the exact missing object or key:
   `kubectl describe pod <pod> -n <ns>` — `Events`: `configmap "<name>" not found` / `secret "<name>" not found` / `couldn't find key <key>`.
3. Confirm existence in the pod's namespace:
   `kubectl get configmap <name> -n <ns>` and `kubectl get secret <name> -n <ns>`
4. Search other namespaces for a misplaced object:
   `kubectl get configmap,secret -A --field-selector metadata.name=<name>`
5. List keys present:
   `kubectl get configmap <name> -n <ns> -o jsonpath='{.data}' ` or `kubectl get secret <name> -n <ns> -o jsonpath='{.data}' | jq 'keys'`
6. List every reference in the workload:
   `kubectl get deploy <deploy> -n <ns> -o yaml | grep -E -A3 'configMapKeyRef|secretKeyRef|configMapRef|secretRef|configMap:|secret:'`
7. If secrets are managed externally, check sync status:
   `kubectl get externalsecret -n <ns>` and `kubectl describe externalsecret <name> -n <ns>` (look for `SecretSyncedError`).
8. Check recent changes: `kubectl rollout history deployment/<deploy> -n <ns>` and Git history for the config.

## Remediation

All remediation actions require human approval before execution.

- **Object missing:** create or re-apply it from source control in the correct namespace:
  `kubectl apply -f config/app-config.yaml -n <ns>`. Pods in `CreateContainerConfigError` retry automatically; if they stay stuck, `rollout_restart`:
  `kubectl rollout restart deployment/<deploy> -n <ns>`.
- **New rollout references a key/object that does not exist yet:** roll back — safe action `rollback_deployment` — then deploy config before the workload.
- **Key renamed:** add the missing key to the ConfigMap/Secret or update the reference.
- **External secret sync failure:** fix the provider credentials or path, force a resync, then `rollout_restart`.
- Note: existing running pods are not affected; env-sourced values are read only at container start, so after changing a ConfigMap/Secret used as env, a `rollout_restart` is required to pick it up.

## Prevention

- Deploy configuration before workloads (Helm templates in the same release, Argo CD sync waves).
- Validate references in CI (kubeconform plus a policy that checks referenced objects exist).
- Use Kustomize generators so names are hashed and references rewritten consistently.
- Use `optional: true` only for truly optional config.
- Alert on `kube_pod_container_status_waiting_reason{reason="CreateContainerConfigError"}`.
- Protect shared Secrets from deletion with ownership labels and RBAC.

## Related

- [crashloopbackoff](crashloopbackoff.md)
- [cert-expiry](cert-expiry.md)
- [failing-rollout](failing-rollout.md)
- [imagepullbackoff](imagepullbackoff.md)
