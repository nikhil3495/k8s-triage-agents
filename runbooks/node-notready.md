---
id: node-notready
title: Node NotReady
category: node_not_ready
severity: sev1
symptoms: ["NotReady", "NodeNotReady", "Kubelet stopped posting node status", "node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable", "Terminating", "Unknown"]
---

## Summary

A node becomes `NotReady` when its `Ready` condition is `False` (kubelet reports it is unhealthy) or `Unknown` (the control plane has not received a heartbeat within `node-monitor-grace-period`, default 40s). The node lifecycle controller then taints the node with `node.kubernetes.io/not-ready:NoExecute` or `node.kubernetes.io/unreachable:NoExecute`; pods without matching tolerations are evicted after `tolerationSeconds` (default 300s). Pods on an unreachable node appear stuck in `Terminating` or `Unknown` because the kubelet cannot confirm deletion. StatefulSet pods are not recreated elsewhere until the node is confirmed gone.

## Symptoms

- `kubectl get nodes`: `STATUS` = `NotReady` (or `NotReady,SchedulingDisabled`).
- `kubectl describe node <node>` Conditions:
  - `Ready  Unknown  ...  NodeStatusUnknown  Kubelet stopped posting node status.`
  - `Ready  False  ...  KubeletNotReady  container runtime network not ready: NetworkReady=false reason:NetworkPluginNotReady message:Network plugin returns error: cni plugin not initialized`
  - `Ready  False  ...  KubeletNotReady  PLEG is not healthy: pleg was last seen active 3m0s ago; threshold is 3m0s`
  - `MemoryPressure True`, `DiskPressure True` (`KubeletHasDiskPressure`), `PIDPressure True`
- Node taints: `node.kubernetes.io/unreachable:NoExecute`, `node.kubernetes.io/not-ready:NoSchedule`, `node.kubernetes.io/disk-pressure:NoSchedule`.
- Events: `Normal  NodeNotReady  node-controller  Node <node> status is now: NodeNotReady`; pod events `Warning  NodeNotReady  Node is not ready`.
- Pods on the node: `Terminating` indefinitely or `STATUS Unknown`; evicted pods with `The node was low on resource: ephemeral-storage`.

## Likely Causes

- kubelet stopped or crashing (bad config, certificate expiry — see `cert-expiry`, OOM-killed by the kernel).
- Container runtime (containerd/CRI-O) hung, causing `PLEG is not healthy`.
- CNI plugin not running on the node (`cni plugin not initialized`).
- Node resource exhaustion: disk full (images, logs, emptyDir), memory exhaustion without system reservations, PID exhaustion.
- Network partition between node and API server; security group / firewall change.
- Underlying VM terminated, stopped, or hardware failure; spot/preemptible instance reclaimed.
- Kernel panic or hung kernel.

## Diagnosis

1. Identify NotReady nodes and their age/version:
   `kubectl get nodes -o wide`
2. Read conditions and their messages:
   `kubectl describe node <node>` — `Conditions` table, `Taints`, and `Events`.
3. Get the Ready condition precisely:
   `kubectl get node <node> -o jsonpath='{.status.conditions[?(@.type=="Ready")]}'`
4. List affected pods on the node:
   `kubectl get pods -A -o wide --field-selector spec.nodeName=<node>`
5. Check node-level agents (CNI, kube-proxy) on that node:
   `kubectl get pods -n kube-system -o wide --field-selector spec.nodeName=<node>`
6. Check node resource pressure (if partially reachable): `kubectl top node <node>`.
7. On the node (SSH/SSM, or `kubectl debug node/<node> -it --image=busybox` if kubelet still works):
   - `systemctl status kubelet containerd`
   - `journalctl -u kubelet --since "30 min ago" | tail -100` — look for `PLEG`, `x509`, `failed to run Kubelet`, `eviction manager`.
   - `df -h /var/lib/kubelet /var/lib/containerd` and `df -i` for inode exhaustion.
   - `free -m`, `dmesg -T | grep -i -E 'oom|panic|hung'`.
8. Check the cloud provider console for instance state (stopped, terminated, failed status checks).

## Remediation

All remediation actions require human approval before execution.

- **Protect workloads first:** cordon the node to stop new scheduling: `kubectl cordon <node>`.
- **kubelet/runtime hung:** restart on the node: `systemctl restart containerd kubelet`.
- **Disk pressure:** prune unused images (`crictl rmi --prune`), clear old logs, then confirm `DiskPressure False`.
- **Node unrecoverable:** drain and replace:
  `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --timeout=5m`, then terminate the instance and let the node group replace it.
- **Pods stuck Terminating on a dead node:** once the VM is confirmed gone, `kubectl delete node <node>` lets controllers recreate pods; force-delete only if needed (`kubectl delete pod <pod> --grace-period=0 --force`) — never force-delete StatefulSet pods while the node might still be running them.
- **Capacity lost:** if remaining nodes are short of capacity, see `pending-pods-insufficient-resources`; `scale_deployment` of non-critical workloads down may free room. `rollout_restart` can rebalance pods after the node returns.

## Prevention

- Configure `kubeReserved`, `systemReserved`, and eviction thresholds so system daemons are never starved.
- Use node auto-repair / machine health checks.
- Monitor disk usage on `/var/lib/containerd` and image GC thresholds.
- Spread replicas across nodes/zones (anti-affinity, topology spread) and use PodDisruptionBudgets.
- Alert on `kube_node_status_condition{condition="Ready",status!="true"}` and node pressure conditions.
- Automate kubelet certificate rotation (`rotateCertificates: true`).

## Related

- [pending-pods-insufficient-resources](pending-pods-insufficient-resources.md)
- [cert-expiry](cert-expiry.md)
- [dns-failure](dns-failure.md)
- [oomkilled](oomkilled.md)
- [pvc-binding](pvc-binding.md)
