---
id: pending-pods-insufficient-resources
title: Pods Pending - Unschedulable (Insufficient Resources)
category: unschedulable
severity: sev2
symptoms: ["Pending", "FailedScheduling", "0/3 nodes are available", "Insufficient cpu", "Insufficient memory", "didn't match Pod's node affinity/selector", "untolerated taint", "PodScheduled False", "Unschedulable"]
---

## Summary

A pod stays in `Pending` with `PodScheduled=False, Reason=Unschedulable` when kube-scheduler cannot find any node satisfying its constraints. The most common cause is that no node has enough unreserved CPU or memory to fit the pod's **requests** (not actual usage). Other filters (node selectors, affinity, taints, topology spread, volume zone, max pods per node) produce similar `FailedScheduling` events. The scheduler's event message lists exactly why each node was rejected.

## Symptoms

- `kubectl get pods`: `STATUS` = `Pending`, `NODE` = `<none>`, for minutes.
- `kubectl describe pod` → Conditions `PodScheduled False`; Events:
  - `Warning  FailedScheduling  default-scheduler  0/3 nodes are available: 3 Insufficient cpu. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod.`
  - `0/5 nodes are available: 2 Insufficient memory, 3 node(s) didn't match Pod's node affinity/selector.`
  - `0/4 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: }, 3 Insufficient cpu.`
  - `0/3 nodes are available: 3 Too many pods.`
  - `0/3 nodes are available: 3 node(s) didn't match pod topology spread constraints.`
  - `0/3 nodes are available: 3 node(s) had volume node affinity conflict.` (see `pvc-binding`)
- Cluster autoscaler events on the pod: `Normal  TriggeredScaleUp  pod triggered scale-up: [{<asg> 3->5 (max: 10)}]` or `Normal  NotTriggerScaleUp  pod didn't trigger scale-up: 2 max node group size reached`.
- HPA scaled up but new replicas never become Ready; Deployment shows `AVAILABLE` < `DESIRED`.

## Likely Causes

- Cluster at capacity: sum of requests on every node too high to fit the new pod.
- Pod requests unrealistically large (e.g. `cpu: 8` on 4-vCPU nodes) — can never fit on any node.
- Cluster autoscaler at max node group size, disabled, or unable to provision (cloud quota, instance type unavailable).
- nodeSelector / nodeAffinity targeting a node pool that has no nodes or is full.
- Taints without matching tolerations (dedicated pools, `node.kubernetes.io/not-ready`, `unreachable`).
- Nodes cordoned (`SchedulingDisabled`) during maintenance.
- ResourceQuota is a separate failure: pods are not created at all (`exceeded quota` on the ReplicaSet), not Pending.

## Diagnosis

1. List pending pods:
   `kubectl get pods -A --field-selector=status.phase=Pending`
2. Read the scheduler's reason:
   `kubectl describe pod <pod> -n <ns>` — the `FailedScheduling` message breaks down per-node rejections.
3. Compare the pod's requests with node allocatable:
   `kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.containers[*].resources.requests}'`
   `kubectl describe nodes | grep -A8 "Allocated resources"` — look at `cpu` / `memory` Requests percentages.
4. Show allocatable per node:
   `kubectl get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory`
5. Check cordoned / not-ready nodes and taints:
   `kubectl get nodes` (look for `SchedulingDisabled`, `NotReady`) and `kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints`
6. Check pod placement constraints:
   `kubectl get pod <pod> -n <ns> -o yaml | grep -A15 -E 'nodeSelector|affinity|tolerations|topologySpreadConstraints'`
7. Check cluster autoscaler status:
   `kubectl get configmap cluster-autoscaler-status -n kube-system -o yaml` and `kubectl logs -n kube-system deploy/cluster-autoscaler --tail=100`
8. If replicas are missing entirely (not Pending), check quota: `kubectl describe resourcequota -n <ns>` and `kubectl describe rs <rs> -n <ns>` for `exceeded quota`.

## Remediation

All remediation actions require human approval before execution.

- **Requests oversized vs actual usage:** lower requests to realistic values — safe action `patch_resources`:
  `kubectl set resources deployment/<deploy> -n <ns> -c <container> --requests=cpu=250m,memory=256Mi`
- **Temporary relief by freeing capacity:** reduce replicas of non-critical workloads — safe action `scale_deployment`. Do not scale down the affected service further.
- **New rollout requests more resources than fit:** `rollback_deployment` to the previous revision while capacity is added.
- **Autoscaler at max:** raise the node group max size or add a node pool; check cloud quotas.
- **Cordoned nodes:** `kubectl uncordon <node>` once maintenance is complete.
- **Affinity/taint mismatch:** fix the nodeSelector/tolerations, or add nodes to the targeted pool.
- Use PriorityClasses so critical pods can preempt lower-priority ones.

## Prevention

- Right-size requests from observed usage (VPA recommendations, Goldilocks).
- Keep cluster headroom (e.g. low-priority placeholder pods) for burst scale-ups.
- Configure cluster autoscaler max sizes with margin; alert when a node group is at max.
- Alert on pods `Pending` > 5 minutes (`kube_pod_status_phase{phase="Pending"}`) and on `FailedScheduling` events.
- Apply LimitRange defaults to avoid pods with no requests skewing bin-packing.

## Related

- [hpa-not-scaling](hpa-not-scaling.md)
- [node-notready](node-notready.md)
- [pvc-binding](pvc-binding.md)
- [failing-rollout](failing-rollout.md)
- [oomkilled](oomkilled.md)
