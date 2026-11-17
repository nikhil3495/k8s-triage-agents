---
id: pvc-binding
title: PersistentVolumeClaim Pending / Volume Binding and Attach Failures
category: pvc_binding
severity: sev2
symptoms: ["Pending", "ProvisioningFailed", "FailedBinding", "no persistent volumes available for this claim and no storage class is set", "waiting for first consumer to be created before binding", "pod has unbound immediate PersistentVolumeClaims", "volume node affinity conflict", "FailedAttachVolume", "FailedMount", "Multi-Attach error"]
---

## Summary

A pod that mounts a PersistentVolumeClaim cannot start until the PVC is `Bound` to a PersistentVolume and the volume is attached and mounted on the node. Failures happen in three stages: **binding/provisioning** (PVC stays `Pending`), **scheduling** (pod `Pending` due to unbound claims or zone conflicts), and **attach/mount** (pod stuck in `ContainerCreating` with `FailedAttachVolume` / `FailedMount`). Each stage has distinct event reasons that identify the problem.

## Symptoms

- `kubectl get pvc`: `STATUS` = `Pending`, `VOLUME` empty.
- `kubectl describe pvc` events:
  - `Normal   WaitForFirstConsumer  waiting for first consumer to be created before binding` (expected until a pod uses it)
  - `Warning  ProvisioningFailed  storageclass.storage.k8s.io "fast-ssd" not found`
  - `Warning  ProvisioningFailed  failed to provision volume with StorageClass "gp3": rpc error: ... UnauthorizedOperation` or `VolumeLimitExceeded`
  - `Normal   ExternalProvisioning  Waiting for a volume to be created either by the external provisioner "ebs.csi.aws.com" or manually by the system administrator` (persisting = CSI controller not working)
  - `Warning  FailedBinding  no persistent volumes available for this claim and no storage class is set`
- Pod events:
  - `Warning  FailedScheduling  0/3 nodes are available: pod has unbound immediate PersistentVolumeClaims.`
  - `0/3 nodes are available: 3 node(s) had volume node affinity conflict.`
  - `Warning  FailedAttachVolume  Multi-Attach error for volume "pvc-..." Volume is already exclusively attached to one node and can't be attached to another`
  - `Warning  FailedMount  MountVolume.MountDevice failed for volume "pvc-..." : rpc error: ... context deadline exceeded`
  - `Unable to attach or mount volumes: unmounted volumes=[data], unattached volumes=[data]: timed out waiting for the condition`
- Pod `STATUS` = `ContainerCreating` for many minutes.

## Likely Causes

- PVC references a `storageClassName` that does not exist, or no default StorageClass exists.
- Static provisioning: no PV matches requested size, `accessModes`, `storageClassName`, or selector.
- CSI driver controller/node pods not running or lacking cloud IAM permissions.
- Cloud quota or per-node volume attach limit reached.
- Zonal volume (EBS, PD) in a different availability zone from any schedulable node (`volume node affinity conflict`), often after node pool changes; StorageClass not using `volumeBindingMode: WaitForFirstConsumer`.
- `ReadWriteOnce` volume still attached to the old node (old pod on a NotReady node, or slow detach) → `Multi-Attach error`.
- Requested access mode not supported by the backend (e.g. `ReadWriteMany` on block storage).

## Diagnosis

1. Check PVC status and class:
   `kubectl get pvc -n <ns>` and `kubectl describe pvc <pvc> -n <ns>`
2. Verify the StorageClass exists and which is default:
   `kubectl get storageclass` — look for `(default)`, `PROVISIONER`, `VOLUMEBINDINGMODE`.
3. For static PVs, list candidates:
   `kubectl get pv` — check `CAPACITY`, `ACCESS MODES`, `STORAGECLASS`, `STATUS` (`Available` vs `Released` — `Released` PVs are not rebound automatically).
4. Check the pod's events for scheduling vs attach vs mount failures:
   `kubectl describe pod <pod> -n <ns>`
5. Check CSI driver health:
   `kubectl get pods -n kube-system | grep -i csi` and `kubectl logs -n kube-system <csi-controller-pod> -c csi-provisioner --tail=100`
6. For zone conflicts, compare PV node affinity with node zones:
   `kubectl get pv <pv> -o jsonpath='{.spec.nodeAffinity}'` and `kubectl get nodes -L topology.kubernetes.io/zone`
7. For Multi-Attach, find where the volume is attached:
   `kubectl get volumeattachment | grep <pv>` and check if the old pod/node still exists.

## Remediation

All remediation actions require human approval before execution.

- **Missing StorageClass:** create it or correct `storageClassName` in the PVC (PVC spec is mostly immutable — delete and recreate the PVC if it never bound).
- **CSI controller failing:** fix IAM/permissions, then restart the controller — `rollout_restart` of the CSI controller Deployment.
- **Zone conflict:** add nodes in the volume's zone, or restore the volume into a zone with capacity; switch StorageClass to `WaitForFirstConsumer` for new volumes.
- **Multi-Attach with old pod on a dead node:** confirm the node is gone, then delete the stale node/pod so the attach-detach controller detaches the volume (after ~6 minutes it force-detaches). For Deployments using RWO volumes, use `strategy: Recreate` instead of `RollingUpdate`.
- **Bad rollout changed volume spec:** `rollback_deployment`.
- Never delete a PVC with `reclaimPolicy: Delete` without a backup — it destroys the data.

## Prevention

- Define exactly one default StorageClass with `volumeBindingMode: WaitForFirstConsumer` and `allowVolumeExpansion: true`.
- Use `Retain` reclaim policy for critical data.
- Monitor CSI driver health and cloud volume quotas.
- Alert on PVCs `Pending` > 5 minutes (`kube_persistentvolumeclaim_status_phase{phase="Pending"}`).
- Use StatefulSets with zone-aware topology spread.

## Related

- [pending-pods-insufficient-resources](pending-pods-insufficient-resources.md)
- [node-notready](node-notready.md)
- [failing-rollout](failing-rollout.md)
- [crashloopbackoff](crashloopbackoff.md)
