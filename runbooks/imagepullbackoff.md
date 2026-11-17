---
id: imagepullbackoff
title: Pod stuck in ImagePullBackOff / ErrImagePull
category: image_pull
severity: sev2
symptoms: ["ImagePullBackOff", "ErrImagePull", "Failed to pull image", "manifest unknown", "not found", "pull access denied", "unauthorized: authentication required", "Back-off pulling image", "toomanyrequests"]
---

## Summary

The kubelet cannot pull the container image for a pod. The first failure shows `ErrImagePull`; subsequent retries back off and the status becomes `ImagePullBackOff` (back-off up to 5 minutes). The container never starts, so there are no application logs. The cause is always in the pod events: wrong image name or tag, missing registry credentials, registry rate limiting, or network/DNS problems between the node and the registry.

## Symptoms

- `kubectl get pods`: `STATUS` = `ErrImagePull` or `ImagePullBackOff`, `READY` = `0/1`, `RESTARTS` = `0`.
- `kubectl describe pod` → `State: Waiting`, `Reason: ImagePullBackOff`.
- Events (`Reason` column `Failed` / `BackOff`):
  - `Failed to pull image "registry.example.com/app:v1.2.3": rpc error: code = NotFound desc = failed to pull and unpack image ... : not found`
  - `Failed to pull image "...": ... manifest unknown`
  - `Failed to pull image "...": ... pull access denied, repository does not exist or may require authorization`
  - `... unauthorized: authentication required` or `403 Forbidden`
  - `... toomanyrequests: You have reached your pull rate limit` (Docker Hub)
  - `... dial tcp: lookup registry.example.com: no such host` or `i/o timeout`
  - `Error: ErrImagePull` then `Back-off pulling image "..."`
- `InvalidImageName`: `Failed to apply default image tag ...: couldn't parse image reference`.

## Likely Causes

- Typo in image repository or tag, or the tag was never pushed (CI build failed, wrong branch).
- Tag deleted or overwritten by registry retention policy.
- Private registry and no `imagePullSecrets` on the pod or ServiceAccount, or the secret is in another namespace.
- Expired registry credentials (e.g. ECR tokens expire after 12 hours; rotated robot accounts).
- Node IAM role lacks registry read permission (ECR/GCR/ACR).
- Docker Hub anonymous pull rate limit.
- Node cannot reach the registry: egress firewall, proxy, private endpoint, or DNS failure.
- Architecture mismatch (`no matching manifest for linux/arm64`).

## Diagnosis

1. Find affected pods:
   `kubectl get pods -n <ns> | grep -E 'ImagePullBackOff|ErrImagePull|InvalidImageName'`
2. Read the exact pull error:
   `kubectl describe pod <pod> -n <ns>` — the `Events` section's `Failed to pull image` message identifies the category (`not found` / `manifest unknown` vs `unauthorized` / `pull access denied` vs `toomanyrequests` vs `no such host` / `i/o timeout`).
3. Print the exact image reference:
   `kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.containers[*].image}'`
4. Check the image exists in the registry from a workstation:
   `crane manifest <image>` or `docker manifest inspect <image>`
5. Check pull secrets on the pod and ServiceAccount:
   `kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.imagePullSecrets}'`
   `kubectl get sa <sa> -n <ns> -o jsonpath='{.imagePullSecrets}'`
6. Validate the secret exists in the same namespace and has the right type (`kubernetes.io/dockerconfigjson`) and registry host:
   `kubectl get secret <secret> -n <ns> -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d`
7. Determine if it is node-specific (network/IAM): compare `NODE` across failing and healthy pods with `kubectl get pods -o wide`.
8. Check whether a recent rollout changed the image: `kubectl rollout history deployment/<deploy> -n <ns>`.

## Remediation

All remediation actions require human approval before execution.

- **Wrong or missing tag from a new rollout:** roll back to the previous working image — safe action `rollback_deployment`:
  `kubectl rollout undo deployment/<deploy> -n <ns>`. Then fix the tag in CI and redeploy.
- **Missing credentials:** create the pull secret in the pod's namespace and reference it:
  `kubectl create secret docker-registry regcred -n <ns> --docker-server=<host> --docker-username=<u> --docker-password=<p>`
  `kubectl patch sa default -n <ns> -p '{"imagePullSecrets":[{"name":"regcred"}]}'`
  Then `rollout_restart` so new pods are created with the secret.
- **Expired credentials:** rotate the secret (or fix the credential helper / IAM role) and `rollout_restart`.
- **Rate limit:** authenticate pulls, mirror images to a private registry, or use a pull-through cache.
- **Network/DNS:** see `dns-failure`; verify egress rules and proxy settings on nodes.
- Existing running pods are unaffected; the failure only hits new pods, so a broken rollout will be stuck mid-way (see `failing-rollout`).

## Prevention

- Promote images by immutable digest (`image@sha256:...`) and verify existence in CI before deploying.
- Use admission policy to block `:latest` and unapproved registries.
- Automate registry credential refresh; prefer node/workload identity over static secrets.
- Mirror third-party images internally.
- Alert on `kube_pod_container_status_waiting_reason{reason=~"ImagePullBackOff|ErrImagePull"}`.

## Related

- [failing-rollout](failing-rollout.md)
- [dns-failure](dns-failure.md)
- [crashloopbackoff](crashloopbackoff.md)
- [missing-configmap-secret](missing-configmap-secret.md)
