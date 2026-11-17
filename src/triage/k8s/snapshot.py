"""Capture a replay snapshot from any :class:`ClusterReader` (normally the live one)."""

from __future__ import annotations

from datetime import UTC, datetime

from triage.k8s.base import JSON, ClusterError, ClusterReader


def capture_snapshot(reader: ClusterReader, scenario: str, namespace: str) -> JSON:
    """Record everything the investigation tools can ask for in one namespace.

    The result is the on-disk format consumed by
    :class:`~triage.k8s.replay.ReplayClusterClient`.
    """
    pods = reader.list_pods(namespace)
    details: dict[str, JSON] = {}
    logs: dict[str, dict[str, str]] = {}
    for pod in pods:
        name = str(pod["name"])
        details[name] = reader.describe_pod(namespace, name)
        logs[name] = {
            "current": reader.get_pod_logs(namespace, name),
            "previous": reader.get_pod_logs(namespace, name, previous=True),
        }
    try:
        pod_metrics = reader.get_pod_metrics(namespace)
        node_metrics = reader.get_node_metrics()
    except ClusterError:
        pod_metrics, node_metrics = [], []
    return {
        "scenario": scenario,
        "namespace": namespace,
        "captured_at": datetime.now(UTC).isoformat(),
        "source": "live-kind",
        "pods": pods,
        "pod_details": details,
        "logs": logs,
        "events": reader.get_events(namespace),
        "deployments": reader.list_deployments(namespace),
        "services": reader.list_services(namespace),
        "nodes": reader.list_nodes(),
        "pod_metrics": pod_metrics,
        "node_metrics": node_metrics,
    }
