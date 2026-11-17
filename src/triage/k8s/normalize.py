"""Turn raw (sanitized) Kubernetes API objects into compact, LLM-friendly dicts.

Both the live client and the fixture recorder go through these functions, so
replay snapshots have exactly the shape the live client produces.
"""

from __future__ import annotations

from typing import Any

JSON = dict[str, Any]


def _container_state(state: JSON | None) -> JSON:
    """Collapse a V1ContainerState dict into ``{state, reason, exit_code, message}``."""
    if not state:
        return {"state": "unknown"}
    for key in ("running", "waiting", "terminated"):
        detail = state.get(key)
        if detail:
            out: JSON = {"state": key}
            for src, dst in (
                ("reason", "reason"),
                ("exitCode", "exit_code"),
                ("message", "message"),
            ):
                if detail.get(src) is not None:
                    out[dst] = detail[src]
            return out
    return {"state": "unknown"}


def pod_summary(pod: JSON) -> JSON:
    """Equivalent of a ``kubectl get pods`` row, plus the waiting/terminated reason."""
    meta = pod.get("metadata", {})
    status = pod.get("status", {})
    statuses = status.get("containerStatuses") or []
    ready = sum(1 for c in statuses if c.get("ready"))
    restarts = sum(int(c.get("restartCount", 0)) for c in statuses)
    reason = status.get("reason")
    for c in statuses:
        st = _container_state(c.get("state"))
        if st["state"] in ("waiting", "terminated") and st.get("reason"):
            reason = st["reason"]
            break
    owner = (meta.get("ownerReferences") or [{}])[0]
    return {
        "name": meta.get("name"),
        "phase": status.get("phase"),
        "ready": f"{ready}/{len(pod.get('spec', {}).get('containers', []))}",
        "restarts": restarts,
        "reason": reason,
        "node": pod.get("spec", {}).get("nodeName"),
        "owner": owner.get("name"),
        "labels": meta.get("labels") or {},
    }


def pod_detail(pod: JSON) -> JSON:
    """Equivalent of ``kubectl describe pod`` (spec essentials + full status)."""
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    containers = []
    statuses = {c.get("name"): c for c in status.get("containerStatuses") or []}
    for c in spec.get("containers", []):
        cs = statuses.get(c.get("name"), {})
        containers.append(
            {
                "name": c.get("name"),
                "image": c.get("image"),
                "command": c.get("command"),
                "args": c.get("args"),
                "resources": c.get("resources") or {},
                "readiness_probe": c.get("readinessProbe"),
                "liveness_probe": c.get("livenessProbe"),
                "env_from": c.get("envFrom"),
                "env": [
                    {k: v for k, v in e.items() if k in ("name", "valueFrom")}
                    for e in c.get("env") or []
                ],
                "ready": cs.get("ready"),
                "restart_count": cs.get("restartCount", 0),
                "state": _container_state(cs.get("state")),
                "last_state": _container_state(cs.get("lastState")),
            }
        )
    return {
        "name": pod.get("metadata", {}).get("name"),
        "namespace": pod.get("metadata", {}).get("namespace"),
        "labels": pod.get("metadata", {}).get("labels") or {},
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "conditions": [
            {k: c.get(k) for k in ("type", "status", "reason", "message") if c.get(k) is not None}
            for c in status.get("conditions") or []
        ],
        "containers": containers,
        "volumes": [v.get("name") for v in spec.get("volumes") or []],
    }


def event_summary(ev: JSON) -> JSON:
    """Compact event row (type, reason, object, message, count)."""
    obj = ev.get("involvedObject", {})
    return {
        "type": ev.get("type"),
        "reason": ev.get("reason"),
        "object": f"{obj.get('kind')}/{obj.get('name')}",
        "message": ev.get("message"),
        "count": ev.get("count") or 1,
        "last_seen": ev.get("lastTimestamp") or ev.get("eventTime"),
    }


def deployment_summary(dep: JSON) -> JSON:
    """Replica counts, revision and rollout conditions for a deployment."""
    meta = dep.get("metadata", {})
    spec = dep.get("spec", {})
    status = dep.get("status", {})
    tmpl = spec.get("template", {}).get("spec", {})
    return {
        "name": meta.get("name"),
        "revision": (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision"),
        "replicas": spec.get("replicas"),
        "ready_replicas": status.get("readyReplicas", 0),
        "updated_replicas": status.get("updatedReplicas", 0),
        "unavailable_replicas": status.get("unavailableReplicas", 0),
        "selector": (spec.get("selector") or {}).get("matchLabels"),
        "containers": [
            {"name": c.get("name"), "image": c.get("image"), "resources": c.get("resources") or {}}
            for c in tmpl.get("containers", [])
        ],
        "conditions": [
            {k: c.get(k) for k in ("type", "status", "reason", "message") if c.get(k) is not None}
            for c in status.get("conditions") or []
        ],
    }


def service_summary(svc: JSON, endpoints: JSON | None) -> JSON:
    """Service selector + how many ready/not-ready endpoint addresses back it."""
    ready = 0
    not_ready = 0
    for subset in (endpoints or {}).get("subsets") or []:
        ready += len(subset.get("addresses") or [])
        not_ready += len(subset.get("notReadyAddresses") or [])
    spec = svc.get("spec", {})
    return {
        "name": svc.get("metadata", {}).get("name"),
        "type": spec.get("type"),
        "selector": spec.get("selector") or {},
        "ports": [
            {k: p.get(k) for k in ("port", "targetPort", "protocol")}
            for p in spec.get("ports") or []
        ],
        "ready_endpoints": ready,
        "not_ready_endpoints": not_ready,
    }


def node_summary(node: JSON) -> JSON:
    """Ready condition, pressure conditions, allocatable and taints."""
    status = node.get("status", {})
    conditions = {c.get("type"): c for c in status.get("conditions") or []}
    ready = conditions.get("Ready", {})
    return {
        "name": node.get("metadata", {}).get("name"),
        "ready": ready.get("status"),
        "ready_reason": ready.get("reason"),
        "pressure": [
            t for t, c in conditions.items() if t != "Ready" and c.get("status") == "True"
        ],
        "allocatable": status.get("allocatable") or {},
        "taints": node.get("spec", {}).get("taints") or [],
    }


def parse_cpu_millicores(value: str) -> float:
    """Parse a Kubernetes CPU quantity (``250m``, ``1``, ``1500000n``) to millicores."""
    v = value.strip()
    if v.endswith("n"):
        return float(v[:-1]) / 1_000_000
    if v.endswith("u"):
        return float(v[:-1]) / 1_000
    if v.endswith("m"):
        return float(v[:-1])
    return float(v) * 1000


_MEM_UNITS = {
    "Ki": 1 / 1024,
    "Mi": 1.0,
    "Gi": 1024.0,
    "Ti": 1024.0 * 1024,
    "K": 1000 / 1024 / 1024,
    "M": 1_000_000 / 1024 / 1024,
    "G": 1_000_000_000 / 1024 / 1024,
}


def parse_memory_mib(value: str) -> float:
    """Parse a Kubernetes memory quantity (``128Mi``, ``1Gi``, ``500M``, bytes) to MiB."""
    v = value.strip()
    for suffix in sorted(_MEM_UNITS, key=len, reverse=True):
        if v.endswith(suffix):
            return float(v[: -len(suffix)]) * _MEM_UNITS[suffix]
    return float(v) / 1024 / 1024


def pod_metrics_summary(item: JSON) -> JSON:
    """Convert a ``metrics.k8s.io`` PodMetrics item to cpu_m / memory_mib per container."""
    return {
        "pod": item.get("metadata", {}).get("name"),
        "containers": [
            {
                "name": c.get("name"),
                "cpu_m": round(parse_cpu_millicores(c.get("usage", {}).get("cpu", "0")), 1),
                "memory_mib": round(parse_memory_mib(c.get("usage", {}).get("memory", "0")), 1),
            }
            for c in item.get("containers") or []
        ],
    }


def node_metrics_summary(item: JSON) -> JSON:
    """Convert a ``metrics.k8s.io`` NodeMetrics item to cpu_m / memory_mib."""
    usage = item.get("usage", {})
    return {
        "node": item.get("metadata", {}).get("name"),
        "cpu_m": round(parse_cpu_millicores(usage.get("cpu", "0")), 1),
        "memory_mib": round(parse_memory_mib(usage.get("memory", "0")), 1),
    }
