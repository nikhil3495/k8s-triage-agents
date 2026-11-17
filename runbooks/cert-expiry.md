---
id: cert-expiry
title: TLS Certificate Expired or Expiring
category: cert_expiry
severity: sev1
symptoms: ["x509: certificate has expired or is not yet valid", "certificate has expired", "NET::ERR_CERT_DATE_INVALID", "tls: failed to verify certificate", "Unable to connect to the server", "cert-manager", "Certificate Ready False", "Issuing", "kubeadm certs"]
---

## Summary

Expired certificates break TLS handshakes, causing outages that look like connectivity failures. Three common scopes in Kubernetes: (1) **workload/ingress certificates** in TLS Secrets (often managed by cert-manager) — external clients see browser or client errors; (2) **webhook/internal mTLS certificates** — admission webhooks or service mesh fail, blocking deployments or inter-service calls; (3) **control plane and kubelet certificates** (kubeadm certs default to 1-year validity) — `kubectl` fails, nodes go `NotReady`. The error string `x509: certificate has expired or is not yet valid` appears in all three. Clock skew can produce the same error with a valid certificate.

## Symptoms

- Clients: `x509: certificate has expired or is not yet valid: current time 2026-11-17T09:00:00Z is after 2026-11-16T23:59:59Z`, `SSL certificate problem: certificate has expired`, browser `NET::ERR_CERT_DATE_INVALID`.
- Ingress controller logs: `SSL certificate ... is expired` or serving the default `Kubernetes Ingress Controller Fake Certificate` when the TLS Secret is missing/invalid.
- cert-manager:
  - `kubectl get certificate` → `READY False`
  - Conditions: `Ready False  ... Issuing certificate as Secret does not exist` or `The certificate has expired`
  - CertificateRequest / Order events: `Failed to finalize Order`, `Waiting on certificate issuance from order ... "pending"`, ACME challenge `Waiting for HTTP-01 challenge propagation: wrong status code '404', expected '200'`.
- Admission webhooks: `Internal error occurred: failed calling webhook "<name>": ... x509: certificate has expired or is not yet valid` on `kubectl apply`.
- Control plane: `kubectl` → `Unable to connect to the server: x509: certificate has expired or is not yet valid`; kubelet logs `part of the existing bootstrap client certificate is expired`; nodes `NotReady`.

## Likely Causes

- cert-manager renewal failed silently (ACME challenge blocked by ingress/firewall/DNS, issuer credentials expired, rate limits `too many certificates already issued`).
- Manually created TLS Secret with no renewal process.
- kubeadm cluster not upgraded within a year, so control plane certs expired.
- kubelet client certificate rotation disabled.
- Webhook CA bundle (`caBundle`) not updated after the webhook's serving cert rotated.
- Node clock skew (NTP failure) making a valid cert appear expired or not yet valid.

## Diagnosis

1. Check the certificate presented by an endpoint:
   `echo | openssl s_client -connect <host>:443 -servername <host> 2>/dev/null | openssl x509 -noout -dates -subject -issuer`
2. Check a TLS Secret's expiry:
   `kubectl get secret <tls-secret> -n <ns> -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -enddate`
3. Check cert-manager resources:
   `kubectl get certificate,certificaterequest,order,challenge -A`
   `kubectl describe certificate <cert> -n <ns>` — look at `Not After`, `Renewal Time`, `Ready` condition.
4. Check cert-manager logs: `kubectl logs -n cert-manager deploy/cert-manager --tail=200`.
5. For webhook failures, find the webhook and its service cert:
   `kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations`
6. For control plane (kubeadm), on a control plane node:
   `kubeadm certs check-expiration`
7. Check kubelet cert on a node: `openssl x509 -noout -enddate -in /var/lib/kubelet/pki/kubelet-client-current.pem`.
8. Rule out clock skew: `date -u` on the node vs a trusted source; `timedatectl status`.

## Remediation

All remediation actions require human approval before execution.

- **cert-manager-managed cert:** fix the underlying issuance error (challenge reachability, issuer secret), then force renewal:
  `cmctl renew <cert> -n <ns>` (or delete the Certificate's Secret to trigger reissue).
- **Manual TLS Secret:** obtain a new certificate and replace the Secret:
  `kubectl create secret tls <name> -n <ns> --cert=tls.crt --key=tls.key --dry-run=client -o yaml | kubectl apply -f -`
- **Workloads that load certs only at startup:** after the Secret is updated, `rollout_restart` the Deployment so pods reload it.
- **Webhook cert expired and blocking applies:** renew the cert/caBundle; as an emergency measure, temporarily set `failurePolicy: Ignore` or delete the webhook configuration (approval required, security impact).
- **kubeadm control plane:** `kubeadm certs renew all` on each control plane node, then restart static pods (kube-apiserver, controller-manager, scheduler, etcd) and update `admin.conf` kubeconfigs.
- **Clock skew:** fix NTP/chrony on the affected nodes.
- `rollback_deployment`, `scale_deployment`, and `patch_resources` do not address expiry.

## Prevention

- Manage all certs with cert-manager; renew at 2/3 of lifetime (default `renewBefore` 1/3).
- Alert on `certmanager_certificate_expiration_timestamp_seconds - time() < 14d` and `certmanager_certificate_ready_status{condition="False"}`.
- Use blackbox exporter `probe_ssl_earliest_cert_expiry` for external endpoints.
- Upgrade kubeadm clusters at least annually (upgrade renews certs) and enable kubelet `rotateCertificates`.
- Run NTP on all nodes.

## Related

- [node-notready](node-notready.md)
- [missing-configmap-secret](missing-configmap-secret.md)
- [failing-rollout](failing-rollout.md)
- [dns-failure](dns-failure.md)
