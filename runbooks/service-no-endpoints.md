---
id: service-no-endpoints
title: Service Has No Endpoints (Broken Selector)
category: service_selector
severity: sev1
symptoms: ["Endpoints: <none>", "no endpoints available for service", "503 Service Unavailable", "connection refused", "no healthy upstream", "selector mismatch", "targetPort", "EndpointSlice empty"]
---

## Summary

A Service routes traffic to pods whose labels match its `spec.selector`. The EndpointSlice controller populates endpoints only with pods that match the selector, are in the same namespace, and are Ready. If the selector does not match any pod's labels (typo, label renamed in a Deployment change, wrong namespace), the Service has **no endpoints** and all traffic to it fails even though pods are `Running` and `Ready`. A related failure is a correct selector with a wrong `targetPort`, which yields endpoints but connection refusals. This runbook covers the selector case; if pods match but are unready, see `readiness-probe-failure`.

## Symptoms

- `kubectl describe svc <svc>` shows `Endpoints: <none>` (or `Endpoints:` blank).
- `kubectl get endpoints <svc>` → `ENDPOINTS <none>`.
- `kubectl get endpointslices -l kubernetes.io/service-name=<svc>` → `ENDPOINTS <unset>`.
- Clients inside the cluster: `curl: (7) Failed to connect to <svc> port 80: Connection refused` or connection timeouts (kube-proxy rejects traffic to a ClusterIP with no endpoints).
- API server proxy (`kubectl get --raw /api/v1/namespaces/<ns>/services/<svc>:<port>/proxy/`) → `Error from server (ServiceUnavailable): no endpoints available for service "<svc>"`.
- Ingress-nginx: `503 Service Temporarily Unavailable`; logs `Service "<ns>/<svc>" does not have any active Endpoint.`
- Envoy/Istio: `503` with `no healthy upstream`, `UH` response flag.
- Cloud LoadBalancer targets all unhealthy.
- Pods themselves: `Running`, `READY 1/1`, no restarts — the workload looks healthy.

## Likely Causes

- Service `spec.selector` and pod template labels differ (e.g. `app: payments-api` vs `app: payments`, `app.kubernetes.io/name` vs `app`).
- Labels changed in a Deployment refactor (Helm chart upgrade changed label conventions) without updating the Service, or vice versa.
- Selector includes an extra label (`version: v1`, `tier: backend`) that new pods no longer carry — common after canary/blue-green switches.
- Service and pods in different namespaces.
- Service with no selector intended to use manually managed Endpoints, which were deleted.
- All matching pods unready (endpoints listed under `notReadyAddresses` only) — see `readiness-probe-failure`.
- `targetPort` name/number does not match the container port (endpoints exist but connections refused).

## Diagnosis

1. Confirm the Service has no endpoints:
   `kubectl get endpoints <svc> -n <ns>` and `kubectl describe svc <svc> -n <ns>` (look for `Endpoints: <none>`).
2. Print the Service selector:
   `kubectl get svc <svc> -n <ns> -o jsonpath='{.spec.selector}'`
3. Test the selector directly against pods:
   `kubectl get pods -n <ns> -l app=<value>,<key2>=<value2>` — `No resources found in <ns> namespace.` confirms a selector mismatch.
4. Show actual pod labels:
   `kubectl get pods -n <ns> --show-labels`
5. Compare with the Deployment's pod template labels:
   `kubectl get deploy <deploy> -n <ns> -o jsonpath='{.spec.template.metadata.labels}'`
6. If pods match, check readiness and ports:
   `kubectl get endpointslices -n <ns> -l kubernetes.io/service-name=<svc> -o yaml` (`conditions.ready`)
   `kubectl get svc <svc> -n <ns> -o jsonpath='{.spec.ports}'` vs `kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.containers[*].ports}'`
7. Check recent changes to either object: `kubectl rollout history deployment/<deploy> -n <ns>`, Helm `helm history <release> -n <ns>`, or GitOps diff.
8. Test connectivity from inside the cluster: `kubectl run tmp --rm -it --restart=Never --image=curlimages/curl -- curl -sv http://<svc>.<ns>.svc.cluster.local:<port>`

## Remediation

All remediation actions require human approval before execution.

- **Selector wrong on the Service:** patch the Service selector to match the pod labels:
  `kubectl patch svc <svc> -n <ns> -p '{"spec":{"selector":{"app":"<correct-value>"}}}'`
  Note that a strategic merge patch adds keys; to remove an extra selector key use `--type=json` with a `remove` op, or re-apply the full manifest. Endpoints populate within seconds.
- **Labels changed by a Deployment rollout:** roll back the Deployment — safe action `rollback_deployment`. Note that `spec.selector` of a Deployment is immutable, so label changes usually mean a new Deployment; fix the Service rather than mutating pods.
- **Pods unready:** see `readiness-probe-failure`; `rollout_restart` or `scale_deployment` do not fix a selector mismatch.
- **targetPort mismatch:** correct `targetPort` (prefer named ports like `targetPort: http`).
- Commit the fix to source control so GitOps does not revert it.

## Prevention

- Template Service selectors and pod labels from the same Helm/Kustomize values (`selectorLabels` helper) so they cannot drift.
- Use stable selector labels (`app.kubernetes.io/name`, `app.kubernetes.io/instance`); never put version labels in Service selectors except intentionally for traffic switching.
- Add CI checks (e.g. kube-linter `dangling-service`) that every Service selector matches a workload.
- Alert on Services with zero ready endpoints: `kube_endpoint_address_available == 0` or `kube_endpoint_address{ready="true"}` absent.
- Smoke-test Service reachability after every deploy.

## Related

- [readiness-probe-failure](readiness-probe-failure.md)
- [failing-rollout](failing-rollout.md)
- [dns-failure](dns-failure.md)
- [crashloopbackoff](crashloopbackoff.md)
