"""Cluster access interfaces.

Investigation tools only ever see :class:`ClusterReader` (read-only). Remediation
goes through :class:`ClusterWriter`, which is *only* reachable from the HITL
executor after an approval (see ``triage.hitl``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

JSON = dict[str, Any]


class ClusterError(RuntimeError):
    """Raised when the cluster (or replay snapshot) cannot answer a query."""


@runtime_checkable
class ClusterReader(Protocol):
    """Read-only view of a Kubernetes cluster, returning normalized JSON dicts."""

    def list_pods(self, namespace: str) -> list[JSON]:
        """Summaries of all pods in ``namespace``."""
        ...

    def describe_pod(self, namespace: str, name: str) -> JSON:
        """Detailed spec + status for one pod (like ``kubectl describe pod``)."""
        ...

    def get_pod_logs(
        self, namespace: str, name: str, *, previous: bool = False, tail_lines: int = 100
    ) -> str:
        """Container logs; ``previous`` returns the last terminated container's logs."""
        ...

    def get_events(self, namespace: str, involved_object: str | None = None) -> list[JSON]:
        """Recent events, optionally filtered to one involved object name."""
        ...

    def list_deployments(self, namespace: str) -> list[JSON]:
        """Deployment summaries including replica counts and rollout conditions."""
        ...

    def list_services(self, namespace: str) -> list[JSON]:
        """Services with their selectors and ready endpoint counts."""
        ...

    def list_nodes(self) -> list[JSON]:
        """Node summaries (conditions, allocatable, taints)."""
        ...

    def get_pod_metrics(self, namespace: str) -> list[JSON]:
        """metrics-server usage per pod/container (cpu millicores, memory MiB)."""
        ...

    def get_node_metrics(self) -> list[JSON]:
        """metrics-server usage per node."""
        ...


@runtime_checkable
class ClusterWriter(Protocol):
    """Mutating operations. Never handed to an agent directly."""

    def rollout_restart(self, namespace: str, deployment: str) -> JSON:
        """Trigger a rolling restart of a deployment."""
        ...

    def scale_deployment(self, namespace: str, deployment: str, replicas: int) -> JSON:
        """Set a deployment's replica count."""
        ...

    def rollback_deployment(self, namespace: str, deployment: str) -> JSON:
        """Roll a deployment back to its previous ReplicaSet revision."""
        ...

    def patch_resources(
        self,
        namespace: str,
        deployment: str,
        container: str,
        requests: dict[str, str] | None,
        limits: dict[str, str] | None,
    ) -> JSON:
        """Change a container's resource requests/limits."""
        ...
