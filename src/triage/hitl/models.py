"""Human-in-the-loop data model: pending actions, risk levels and dry-run previews."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from triage.k8s.base import ClusterError, ClusterReader


class ActionType(StrEnum):
    """The only cluster mutations the system can ever request."""

    ROLLOUT_RESTART = "rollout_restart"
    SCALE_DEPLOYMENT = "scale_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    PATCH_RESOURCES = "patch_resources"


class Risk(StrEnum):
    """Approval risk level shown to the approver."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionStatus(StrEnum):
    """Lifecycle of a pending action."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class PendingAction(BaseModel):
    """A remediation waiting for (or past) a human decision."""

    id: str
    investigation_id: str | None = None
    action: ActionType
    namespace: str
    deployment: str
    params: dict[str, Any] = Field(default_factory=dict)
    preview: str
    risk: Risk
    rationale: str = ""
    requested_by: str
    status: ActionStatus = ActionStatus.PENDING
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    result: dict[str, Any] | None = None


def assess_risk(action: ActionType, params: dict[str, Any], current_replicas: int | None) -> Risk:
    """Heuristic risk level: anything that can remove capacity is HIGH."""
    if action == ActionType.SCALE_DEPLOYMENT:
        target = int(params.get("replicas", 0))
        if target == 0 or (current_replicas is not None and target < current_replicas):
            return Risk.HIGH
        return Risk.MEDIUM
    if action == ActionType.ROLLOUT_RESTART:
        return Risk.LOW
    return Risk.MEDIUM


def validate_params(action: ActionType, params: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise action parameters; raises ValueError on bad input."""
    if action == ActionType.SCALE_DEPLOYMENT:
        replicas = int(params.get("replicas", -1))
        if not 0 <= replicas <= 50:
            raise ValueError("scale_deployment needs 0 <= replicas <= 50")
        return {"replicas": replicas}
    if action == ActionType.PATCH_RESOURCES:
        container = str(params.get("container", "")).strip()
        requests = params.get("requests") or None
        limits = params.get("limits") or None
        if not container or not (requests or limits):
            raise ValueError("patch_resources needs container and requests and/or limits")
        allowed = {"cpu", "memory"}
        for block in (requests or {}, limits or {}):
            if set(block) - allowed:
                raise ValueError("only cpu and memory can be patched")
        return {"container": container, "requests": requests, "limits": limits}
    return {}


def build_preview(
    reader: ClusterReader | None,
    action: ActionType,
    namespace: str,
    deployment: str,
    params: dict[str, Any],
) -> tuple[str, int | None]:
    """Dry-run: describe exactly what would change, using current cluster state."""
    current: dict[str, Any] | None = None
    if reader is not None:
        try:
            current = next(
                (d for d in reader.list_deployments(namespace) if d.get("name") == deployment),
                None,
            )
        except ClusterError:
            current = None
    replicas = None if current is None else current.get("replicas")
    head = f"deployment/{deployment} -n {namespace}"
    if current is None:
        head += " (current state unavailable)"
    if action == ActionType.ROLLOUT_RESTART:
        return f"{head}\n~ spec.template.metadata.annotations.restartedAt: <now>", replicas
    if action == ActionType.SCALE_DEPLOYMENT:
        return f"{head}\n~ spec.replicas: {replicas} -> {params['replicas']}", replicas
    if action == ActionType.ROLLBACK_DEPLOYMENT:
        rev = None if current is None else current.get("revision")
        images = [] if current is None else [c.get("image") for c in current.get("containers", [])]
        return (
            f"{head}\n~ revision: {rev} -> previous ReplicaSet revision\n"
            f"- current images: {images}",
            replicas,
        )
    old: dict[str, Any] = {}
    if current is not None:
        for c in current.get("containers", []):
            if c.get("name") == params["container"]:
                old = c.get("resources") or {}
    lines = [head, f"container {params['container']}:"]
    for kind in ("requests", "limits"):
        for key, new in (params.get(kind) or {}).items():
            lines.append(f"~ resources.{kind}.{key}: {(old.get(kind) or {}).get(key)} -> {new}")
    return "\n".join(lines), replicas
