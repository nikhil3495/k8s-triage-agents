"""Replay cluster access from JSON snapshots recorded on a real kind cluster.

Snapshots are written by ``scripts/record_scenario.py`` using
:class:`~triage.k8s.live.LiveClusterClient`, so the data is exactly what the
live client returned while the fault was active.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from triage.k8s.base import JSON, ClusterError

SNAPSHOT_KEYS = (
    "pods",
    "pod_details",
    "logs",
    "events",
    "deployments",
    "services",
    "nodes",
    "pod_metrics",
    "node_metrics",
)


def load_snapshot(path: Path) -> JSON:
    """Load and minimally validate a snapshot file."""
    data: JSON = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("scenario", "namespace", *SNAPSHOT_KEYS) if k not in data]
    if missing:
        raise ClusterError(f"snapshot {path.name} missing keys: {missing}")
    return data


def list_scenarios(fixtures_dir: Path) -> list[str]:
    """Names of all recorded scenarios in ``fixtures_dir``."""
    return sorted(p.stem for p in fixtures_dir.glob("*.json"))


class ReplayClusterClient:
    """A :class:`ClusterReader` backed by one recorded snapshot."""

    def __init__(self, snapshot: JSON) -> None:
        """Wrap an already-loaded snapshot dict."""
        self.snapshot = snapshot
        self.namespace: str = snapshot["namespace"]
        self.scenario: str = snapshot["scenario"]

    @classmethod
    def from_scenario(cls, fixtures_dir: Path, scenario: str) -> ReplayClusterClient:
        """Load ``<fixtures_dir>/<scenario>.json``."""
        path = fixtures_dir / f"{scenario}.json"
        if not path.is_file():
            raise ClusterError(
                f"unknown scenario {scenario!r}; available: {list_scenarios(fixtures_dir)}"
            )
        return cls(load_snapshot(path))

    def _check_ns(self, namespace: str) -> None:
        if namespace != self.namespace:
            raise ClusterError(
                f"namespace {namespace!r} not in snapshot (recorded namespace: {self.namespace!r})"
            )

    def _get(self, key: str) -> Any:
        return self.snapshot[key]

    def list_pods(self, namespace: str) -> list[JSON]:
        """See :meth:`ClusterReader.list_pods`."""
        self._check_ns(namespace)
        return list(self._get("pods"))

    def describe_pod(self, namespace: str, name: str) -> JSON:
        """See :meth:`ClusterReader.describe_pod`."""
        self._check_ns(namespace)
        details: dict[str, JSON] = self._get("pod_details")
        if name not in details:
            raise ClusterError(f'pods "{name}" not found')
        return details[name]

    def get_pod_logs(
        self, namespace: str, name: str, *, previous: bool = False, tail_lines: int = 100
    ) -> str:
        """See :meth:`ClusterReader.get_pod_logs`."""
        self._check_ns(namespace)
        logs: dict[str, dict[str, str]] = self._get("logs")
        if name not in logs:
            raise ClusterError(f'pods "{name}" not found')
        text = logs[name].get("previous" if previous else "current") or ""
        return "\n".join(text.splitlines()[-tail_lines:])

    def get_events(self, namespace: str, involved_object: str | None = None) -> list[JSON]:
        """See :meth:`ClusterReader.get_events`."""
        self._check_ns(namespace)
        events: list[JSON] = self._get("events")
        if involved_object:
            return [e for e in events if str(e.get("object", "")).endswith(f"/{involved_object}")]
        return list(events)

    def list_deployments(self, namespace: str) -> list[JSON]:
        """See :meth:`ClusterReader.list_deployments`."""
        self._check_ns(namespace)
        return list(self._get("deployments"))

    def list_services(self, namespace: str) -> list[JSON]:
        """See :meth:`ClusterReader.list_services`."""
        self._check_ns(namespace)
        return list(self._get("services"))

    def list_nodes(self) -> list[JSON]:
        """See :meth:`ClusterReader.list_nodes`."""
        return list(self._get("nodes"))

    def get_pod_metrics(self, namespace: str) -> list[JSON]:
        """See :meth:`ClusterReader.get_pod_metrics`."""
        self._check_ns(namespace)
        return list(self._get("pod_metrics"))

    def get_node_metrics(self) -> list[JSON]:
        """See :meth:`ClusterReader.get_node_metrics`."""
        return list(self._get("node_metrics"))


class ReplayClusterWriter:
    """Simulated writer for replay mode: records what *would* have been done."""

    def __init__(self) -> None:
        """Start with an empty action log."""
        self.applied: list[JSON] = []

    def _record(self, op: str, **kwargs: Any) -> JSON:
        entry = {"op": op, "simulated": True, **kwargs}
        self.applied.append(entry)
        return {"ok": True, **entry}

    def rollout_restart(self, namespace: str, deployment: str) -> JSON:
        """Record a simulated rollout restart."""
        return self._record("rollout_restart", namespace=namespace, deployment=deployment)

    def scale_deployment(self, namespace: str, deployment: str, replicas: int) -> JSON:
        """Record a simulated scale."""
        return self._record(
            "scale_deployment", namespace=namespace, deployment=deployment, replicas=replicas
        )

    def rollback_deployment(self, namespace: str, deployment: str) -> JSON:
        """Record a simulated rollback."""
        return self._record("rollback_deployment", namespace=namespace, deployment=deployment)

    def patch_resources(
        self,
        namespace: str,
        deployment: str,
        container: str,
        requests: dict[str, str] | None,
        limits: dict[str, str] | None,
    ) -> JSON:
        """Record a simulated resource patch."""
        return self._record(
            "patch_resources",
            namespace=namespace,
            deployment=deployment,
            container=container,
            requests=requests,
            limits=limits,
        )
