"""The supervisor's only write-path tool: it files a request for human approval.

There is deliberately no tool that changes the cluster. Execution happens only
through ``POST /approvals/{id}`` with the approver token (see ``triage.hitl``).
"""

from __future__ import annotations

from google.adk.tools.tool_context import ToolContext

from triage import state_keys
from triage.k8s.base import JSON
from triage.tools.registry import registry


def propose_remediation(
    action: str,
    deployment: str,
    rationale: str,
    tool_context: ToolContext,
    replicas: int = -1,
    container: str = "",
    cpu_limit: str = "",
    memory_limit: str = "",
    cpu_request: str = "",
    memory_request: str = "",
) -> JSON:
    """Request a remediation. It is queued for human approval and NOT executed now.

    Args:
        action: One of rollout_restart, scale_deployment, rollback_deployment, patch_resources.
        deployment: Name of the deployment to act on.
        rationale: Why this fixes the root cause (shown to the approver).
        replicas: Target replica count (scale_deployment only).
        container: Container name (patch_resources only).
        cpu_limit: New CPU limit, e.g. "500m" (patch_resources only).
        memory_limit: New memory limit, e.g. "256Mi" (patch_resources only).
        cpu_request: New CPU request (patch_resources only).
        memory_request: New memory request (patch_resources only).

    """
    from triage.runtime import get_runtime

    params: JSON = {}
    if action == "scale_deployment":
        params["replicas"] = replicas
    if action == "patch_resources":
        params["container"] = container
        params["limits"] = {
            k: v for k, v in (("cpu", cpu_limit), ("memory", memory_limit)) if v
        } or None
        params["requests"] = {
            k: v for k, v in (("cpu", cpu_request), ("memory", memory_request)) if v
        } or None
    state = tool_context.state
    try:
        pa = get_runtime().approvals.propose(
            action=action,
            namespace=str(state.get(state_keys.NAMESPACE, "default")),
            deployment=deployment,
            params=params,
            requested_by=f"agent:{tool_context.agent_name}",
            rationale=rationale,
            investigation_id=state.get("investigation_id"),
            reader=registry.for_state(state),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    ids = list(state.get(state_keys.PROPOSED_ACTIONS) or [])
    ids.append(pa.id)
    state[state_keys.PROPOSED_ACTIONS] = ids
    return {
        "pending_action_id": pa.id,
        "status": "pending_human_approval",
        "risk": pa.risk,
        "preview": pa.preview,
        "note": "Not executed. A human must approve it via POST /approvals/{id}.",
    }
