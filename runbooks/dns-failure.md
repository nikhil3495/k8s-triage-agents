---
id: dns-failure
title: In-Cluster DNS Resolution Failure (CoreDNS)
category: dns
severity: sev1
symptoms: ["no such host", "Temporary failure in name resolution", "i/o timeout", "SERVFAIL", "NXDOMAIN", "could not resolve host", "CoreDNS", "kube-dns", "dial udp 10.96.0.10:53"]
---

## Summary

Pods resolve names through the cluster DNS Service (`kube-dns` in `kube-system`, typically ClusterIP `10.96.0.10` or `172.20.0.10`), backed by CoreDNS pods. When CoreDNS is down, overloaded, misconfigured, or unreachable, applications fail to resolve Service names (`<svc>.<ns>.svc.cluster.local`) and external hostnames. Because nearly every workload depends on DNS, this is usually a cluster-wide, high-severity incident that shows up as connection errors in many apps at once and can trigger secondary `CrashLoopBackOff` and readiness failures.

## Symptoms

- Application logs:
  - `dial tcp: lookup orders-db.prod.svc.cluster.local on 10.96.0.10:53: no such host`
  - `dial tcp: lookup api.example.com on 10.96.0.10:53: read udp 10.244.1.5:41234->10.96.0.10:53: i/o timeout`
  - `getaddrinfo EAI_AGAIN`, `Temporary failure in name resolution`, `Could not resolve host`
  - `java.net.UnknownHostException`
- `nslookup kubernetes.default` from a pod returns `;; connection timed out; no servers could be reached` or `SERVFAIL`.
- `kubectl get pods -n kube-system -l k8s-app=kube-dns` shows CoreDNS `CrashLoopBackOff`, `Pending`, `0/1 Running`, or `OOMKilled`.
- CoreDNS logs: `[ERROR] plugin/errors: 2 <name>. A: read udp ...: i/o timeout`, `[FATAL] plugin/loop: Loop (127.0.0.1:xxxx -> :53) detected for zone "."`, `HINFO: unreachable backend`.
- `kubectl get endpoints kube-dns -n kube-system` shows `<none>`.
- Intermittent 5-second delays on lookups (conntrack race on UDP, or `ndots:5` search-path amplification).

## Likely Causes

- CoreDNS pods crashing: loop detected (node `/etc/resolv.conf` points to `127.0.0.53` systemd-resolved stub), bad Corefile edit, OOMKilled.
- CoreDNS under-provisioned for query volume (too few replicas, CPU throttled).
- Upstream resolvers (VPC DNS, on-prem) unreachable or rate-limiting (AWS VPC resolver: 1024 packets/s per ENI).
- NetworkPolicy blocking egress to `kube-system` on UDP/TCP 53.
- kube-proxy / CNI failure on a node so the `kube-dns` ClusterIP is not routed.
- Pod `dnsPolicy` set to `Default` (uses node resolver, cannot resolve cluster names) or custom `dnsConfig` wrong.
- `ndots:5` causing many search-domain queries for external names, overloading CoreDNS.
- Stub domain / forward config pointing to a dead server.

## Diagnosis

1. Check CoreDNS pod health:
   `kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide`
2. Check the DNS Service has endpoints:
   `kubectl get svc,endpoints kube-dns -n kube-system`
3. Read CoreDNS logs for `[ERROR]`, `[FATAL] plugin/loop`, `i/o timeout`:
   `kubectl logs -n kube-system -l k8s-app=kube-dns --tail=200`
4. Test resolution from a throwaway pod:
   `kubectl run dnsutils --rm -it --restart=Never --image=registry.k8s.io/e2e-test-images/jessie-dnsutils:1.3 -- nslookup kubernetes.default`
   Then `nslookup <svc>.<ns>.svc.cluster.local` and `nslookup example.com` to separate cluster vs upstream failures.
5. Inspect the failing pod's resolver config:
   `kubectl exec -n <ns> <pod> -- cat /etc/resolv.conf` — expect `nameserver <kube-dns ClusterIP>`, `search <ns>.svc.cluster.local svc.cluster.local cluster.local`, `options ndots:5`.
6. Review the Corefile:
   `kubectl get configmap coredns -n kube-system -o yaml` — check `forward . /etc/resolv.conf` or upstream IPs, `loop`, `cache`.
7. Check resource pressure on CoreDNS: `kubectl top pods -n kube-system -l k8s-app=kube-dns`.
8. Check for NetworkPolicies affecting DNS egress: `kubectl get networkpolicy -A`.
9. If only some nodes fail, check kube-proxy/CNI pods there: `kubectl get pods -n kube-system -o wide --field-selector spec.nodeName=<node>`.

## Remediation

All remediation actions require human approval before execution.

- **CoreDNS crashlooping after Corefile change:** revert the `coredns` ConfigMap to the last good version, then `rollout_restart`:
  `kubectl rollout restart deployment/coredns -n kube-system`
- **CoreDNS overloaded:** add replicas — safe action `scale_deployment`:
  `kubectl scale deployment/coredns -n kube-system --replicas=<n>`; or raise CPU/memory — safe action `patch_resources`. Consider the cluster-proportional-autoscaler.
- **Loop detected:** point CoreDNS `forward` at real upstream IPs instead of `/etc/resolv.conf`, or fix kubelet `--resolv-conf` to `/run/systemd/resolve/resolv.conf`.
- **NetworkPolicy:** add an egress rule allowing UDP and TCP 53 to `kube-system` pods labeled `k8s-app: kube-dns`.
- **Node-local routing failure:** restart kube-proxy/CNI on the node or cordon and drain it.
- After DNS recovers, apps that crashed on startup may still be in back-off; `rollout_restart` affected Deployments.

## Prevention

- Run at least 2 CoreDNS replicas with a PodDisruptionBudget and anti-affinity across nodes/zones.
- Deploy NodeLocal DNSCache to reduce latency and conntrack races.
- Use fully-qualified names with a trailing dot or lower `ndots` (e.g. `ndots:2`) for external-heavy workloads.
- Alert on `coredns_dns_responses_total{rcode="SERVFAIL"}`, `coredns_forward_healthcheck_failures_total`, and CoreDNS pod readiness.
- Change-manage the Corefile; validate before apply.

## Related

- [crashloopbackoff](crashloopbackoff.md)
- [readiness-probe-failure](readiness-probe-failure.md)
- [service-no-endpoints](service-no-endpoints.md)
- [node-notready](node-notready.md)
- [imagepullbackoff](imagepullbackoff.md)
