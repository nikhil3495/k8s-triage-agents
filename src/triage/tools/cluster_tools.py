"""Read-only investigation tools exposed to the Log and Metrics agents.

Each function is wrapped by ADK as a FunctionTool: its signature becomes the
tool schema and its docstring the tool description the model sees. Tools never
raise into the model loop; failures come back as ``{"error": ...}`` observations
so the ReAct loop can reason about them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from google.adk.tools.tool_context import ToolContext

from triage.k8s.base import JSON, ClusterError, ClusterReader
from triage.k8s.normalize import parse_cpu_millicores, parse_memory_mib
from triage.tools.registry import registry

log = logging.getLogger(__name__)

MAX_LOG_CHARS = 4000
MAX_ITEMS = 50


def _reader(ctx: ToolContext) -> ClusterReader:
    return registry.for_state(ctx.state)


def _safe(fn: Callable[[], JSON]) -> JSON:
    try:
        return fn()
    except ClusterError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # a tool must never crash the ReAct loop
        log.warning("tool failed", exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def list_pods(namespace: str, tool_context: ToolContext) -> JSON:
    """List pods in a namespace with phase, ready count, restarts and waiting reason.

    Args:
        namespace: Kubernetes namespace to inspect.

    """
    return _safe(lambda: {"pods": _reader(tool_context).list_pods(namespace)[:MAX_ITEMS]})


def describe_pod(namespace: str, pod_name: str, tool_context: ToolContext) -> JSON:
    """Describe one pod: images, commands, probes, resources, env refs, container states.

    Args:
        namespace: Kubernetes namespace of the pod.
        pod_name: Exact pod name (from list_pods).

    """
    return _safe(lambda: {"pod": _reader(tool_context).describe_pod(namespace, pod_name)})


def get_pod_logs(namespace: str, pod_name: str, previous: bool, tool_context: ToolContext) -> JSON:
    """Fetch the last lines of a pod's logs.

    Args:
        namespace: Kubernetes namespace of the pod.
        pod_name: Exact pod name (from list_pods).
        previous: True to read the previous (crashed) container's logs.

    """

    def run() -> JSON:
        text = _reader(tool_context).get_pod_logs(namespace, pod_name, previous=previous)
        truncated = len(text) > MAX_LOG_CHARS
        return {"logs": text[-MAX_LOG_CHARS:], "truncated": truncated, "previous": previous}

    return _safe(run)


def get_events(namespace: str, object_name: str, tool_context: ToolContext) -> JSON:
    """List recent Kubernetes events in a namespace (Warning events first).

    Args:
        namespace: Kubernetes namespace.
        object_name: Only events for this object (pod/deployment name); "" for all.

    """

    def run() -> JSON:
        evs = _reader(tool_context).get_events(namespace, object_name or None)
        evs = sorted(evs, key=lambda e: (e.get("type") != "Warning", -int(e.get("count") or 1)))
        return {"events": evs[:MAX_ITEMS]}

    return _safe(run)


def list_services(namespace: str, tool_context: ToolContext) -> JSON:
    """List services with their label selectors and number of ready endpoints.

    A service with ready_endpoints == 0 while matching pods are Running usually has a
    selector that does not match the pod labels.

    Args:
        namespace: Kubernetes namespace.

    """

    def run() -> JSON:
        reader = _reader(tool_context)
        services = reader.list_services(namespace)
        pod_labels = [p.get("labels") or {} for p in reader.list_pods(namespace)]
        for svc in services:
            sel: dict[str, str] = svc.get("selector") or {}
            svc["pods_matching_selector"] = sum(
                1
                for labels in pod_labels
                if sel and all(labels.get(k) == v for k, v in sel.items())
            )
        return {"services": services}

    return _safe(run)


def list_deployments(namespace: str, tool_context: ToolContext) -> JSON:
    """List deployments with desired/ready/unavailable replicas, revision and conditions.

    Args:
        namespace: Kubernetes namespace.

    """
    return _safe(lambda: {"deployments": _reader(tool_context).list_deployments(namespace)})


def get_restart_counts(namespace: str, tool_context: ToolContext) -> JSON:
    """Restart counts and last termination reason per container, highest first.

    Args:
        namespace: Kubernetes namespace.

    """

    def run() -> JSON:
        reader = _reader(tool_context)
        rows: list[JSON] = []
        for pod in reader.list_pods(namespace):
            detail = reader.describe_pod(namespace, str(pod["name"]))
            for c in detail.get("containers", []):
                rows.append(
                    {
                        "pod": pod["name"],
                        "container": c.get("name"),
                        "restarts": c.get("restart_count", 0),
                        "state": c.get("state"),
                        "last_termination": c.get("last_state"),
                    }
                )
        rows.sort(key=lambda r: -int(r["restarts"] or 0))
        return {"containers": rows[:MAX_ITEMS]}

    return _safe(run)


def _pct(usage: float, limit: str | None, parse: Callable[[str], float]) -> float | None:
    if not limit:
        return None
    lim = parse(limit)
    return round(100 * usage / lim, 1) if lim else None


def get_pod_resource_usage(namespace: str, tool_context: ToolContext) -> JSON:
    """Compare live CPU/memory usage (metrics-server) with each container's requests/limits.

    cpu_pct_of_limit near or above ~90% indicates CPU throttling; memory near the
    limit precedes OOMKilled.

    Args:
        namespace: Kubernetes namespace.

    """

    def run() -> JSON:
        reader = _reader(tool_context)
        usage = {m["pod"]: m for m in reader.get_pod_metrics(namespace)}
        rows: list[JSON] = []
        for pod in reader.list_pods(namespace):
            detail = reader.describe_pod(namespace, str(pod["name"]))
            metrics = {c["name"]: c for c in usage.get(pod["name"], {}).get("containers", [])}
            for c in detail.get("containers", []):
                res: dict[str, Any] = c.get("resources") or {}
                limits = res.get("limits") or {}
                m = metrics.get(c.get("name"), {})
                cpu = m.get("cpu_m")
                mem = m.get("memory_mib")
                rows.append(
                    {
                        "pod": pod["name"],
                        "container": c.get("name"),
                        "cpu_m": cpu,
                        "memory_mib": mem,
                        "requests": res.get("requests") or {},
                        "limits": limits,
                        "cpu_pct_of_limit": None
                        if cpu is None
                        else _pct(cpu, limits.get("cpu"), parse_cpu_millicores),
                        "memory_pct_of_limit": None
                        if mem is None
                        else _pct(mem, limits.get("memory"), parse_memory_mib),
                    }
                )
        return {"containers": rows[:MAX_ITEMS], "metrics_available": bool(usage)}

    def guarded() -> JSON:
        try:
            return run()
        except ClusterError as exc:
            return {"error": str(exc), "hint": "metrics-server may be missing; use restarts/events"}

    return guarded()


def get_node_status(tool_context: ToolContext) -> JSON:
    """Node readiness, pressure conditions, allocatable capacity, taints and live usage."""

    def run() -> JSON:
        reader = _reader(tool_context)
        nodes = reader.list_nodes()
        try:
            usage = {m["node"]: m for m in reader.get_node_metrics()}
        except ClusterError:
            usage = {}
        for n in nodes:
            n["usage"] = usage.get(n["name"])
        return {"nodes": nodes}

    return _safe(run)


LOG_TOOLS: list[Callable[..., JSON]] = [
    list_pods,
    describe_pod,
    get_pod_logs,
    get_events,
    list_services,
]
METRICS_TOOLS: list[Callable[..., JSON]] = [
    get_pod_resource_usage,
    get_restart_counts,
    get_node_status,
    list_deployments,
]
