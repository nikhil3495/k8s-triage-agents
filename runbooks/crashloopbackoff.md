---
id: crashloopbackoff
title: Pod in CrashLoopBackOff
category: crashloop
severity: sev2
symptoms: ["CrashLoopBackOff", "Back-off restarting failed container", "BackOff", "Exit Code: 1", "RESTARTS increasing", "Last State: Terminated"]
---

## Summary

A pod is in `CrashLoopBackOff` when one of its containers starts, exits (with any exit code), and the kubelet restarts it repeatedly. After each failure the kubelet waits an exponentially increasing back-off delay (10s, 20s, 40s ... capped at 5 minutes) before the next restart. The status itself is not the root cause; it only says the container process keeps terminating. The cause is almost always found in the previous container's logs, its exit code, or its `Last State` in `kubectl describe pod`.

## Symptoms

- `kubectl get pods` shows `STATUS` = `CrashLoopBackOff` (or briefly `Error` / `Completed` between restarts) and a growing `RESTARTS` count, e.g. `api-7d9f8b6c4-x2klm   0/1   CrashLoopBackOff   7 (2m ago)   14m`.
- `kubectl describe pod` shows:
  - `State: Waiting` with `Reason: CrashLoopBackOff`
  - `Last State: Terminated` with `Reason: Error` and `Exit Code: 1` (or `2`, `126`, `127`, `139`, `143`)
  - `Ready: False`, `Restart Count: N`
- Events: `Warning  BackOff  kubelet  Back-off restarting failed container <name> in pod <pod>`.
- If `Exit Code: 137` with `Reason: OOMKilled`, follow the `oomkilled` runbook instead.
- If the container never starts and the reason is `CreateContainerConfigError`, follow `missing-configmap-secret`.

## Likely Causes

- Application error on startup: unhandled exception, failed config parse, invalid flag (`Exit Code: 1` or `2`).
- Missing or wrong environment variable / config value (DB URL, feature flag) causing the app to exit.
- Dependency unavailable at boot (database, message broker, DNS) and the app exits instead of retrying.
- Wrong `command`/`args` or entrypoint: `Exit Code: 127` (command not found) or `126` (not executable / permission denied).
- Segmentation fault: `Exit Code: 139` (SIGSEGV).
- Liveness probe killing a slow-starting container: events show `Liveness probe failed` followed by `Container <name> failed liveness probe, will be restarted`; exit code typically `137` or `143` without `OOMKilled`.
- Container runs a short-lived process (e.g. a script) under a Deployment, so it exits `0` (`Reason: Completed`) and is restarted forever.
- Bad image tag rolled out containing a regression.

## Diagnosis

1. Identify crashing pods and restart counts:
   `kubectl get pods -n <ns> -o wide | grep -E 'CrashLoopBackOff|Error'`
2. Inspect container state and exit code:
   `kubectl describe pod <pod> -n <ns>` — read `Last State: Terminated`, `Reason`, `Exit Code`, `Started`/`Finished` timestamps (a few seconds apart means the process dies at boot).
3. Read logs from the previous (crashed) instance — the current one is often empty:
   `kubectl logs <pod> -n <ns> -c <container> --previous`
4. Check events for probe kills or back-off:
   `kubectl get events -n <ns> --field-selector involvedObject.name=<pod> --sort-by=.lastTimestamp`
5. Get the exit code programmatically:
   `kubectl get pod <pod> -n <ns> -o jsonpath='{.status.containerStatuses[*].lastState.terminated.exitCode}'`
6. Check whether this started with a new rollout:
   `kubectl rollout history deployment/<deploy> -n <ns>` and compare the image with
   `kubectl get deploy <deploy> -n <ns> -o jsonpath='{.spec.template.spec.containers[*].image}'`
7. Verify command, args, env, and probes in the spec:
   `kubectl get deploy <deploy> -n <ns> -o yaml` — look at `command`, `args`, `env`, `envFrom`, `livenessProbe.initialDelaySeconds`, `startupProbe`.
8. If logs are empty, debug interactively with an ephemeral container or a copy with a sleep entrypoint:
   `kubectl debug <pod> -n <ns> -it --copy-to=<pod>-debug --container=<container> -- sh`

## Remediation

All remediation actions require human approval before execution.

- **Regression from a new image/config (most common):** roll back to the last known-good revision — safe action `rollback_deployment`:
  `kubectl rollout undo deployment/<deploy> -n <ns>` (optionally `--to-revision=<n>`).
- **Bad config value / env var:** fix the ConfigMap, Secret, or env in the Deployment, then restart pods so they pick up the change — safe action `rollout_restart`:
  `kubectl rollout restart deployment/<deploy> -n <ns>`.
- **Transient dependency outage:** once the dependency is healthy, `rollout_restart` clears the back-off immediately instead of waiting up to 5 minutes.
- **Liveness probe killing slow start:** add a `startupProbe` or raise `initialDelaySeconds` / `failureThreshold` on the liveness probe and redeploy.
- **Exit 137 with OOMKilled:** increase memory limits — safe action `patch_resources` (see `oomkilled`).
- **Wrong entrypoint (126/127):** correct `command`/`args` or the image `ENTRYPOINT`; rebuild if needed.
- Do not delete pods repeatedly as a fix; the ReplicaSet recreates them with the same spec and they crash again.

## Prevention

- Gate rollouts with readiness probes and `progressDeadlineSeconds` so a crashing version does not replace healthy pods.
- Make applications retry dependencies with back-off instead of exiting on first failure.
- Validate config at CI time (schema checks, `--dry-run=server`).
- Use `startupProbe` for slow-booting apps; keep liveness probes cheap and lenient.
- Alert on `kube_pod_container_status_restarts_total` rate and on `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}`.
- Pin image tags by digest; avoid `latest`.

## Related

- [oomkilled](oomkilled.md)
- [missing-configmap-secret](missing-configmap-secret.md)
- [readiness-probe-failure](readiness-probe-failure.md)
- [failing-rollout](failing-rollout.md)
- [dns-failure](dns-failure.md)
