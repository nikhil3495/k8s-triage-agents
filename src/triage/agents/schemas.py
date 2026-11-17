"""Structured outputs exchanged between the supervisor, critic, API and eval."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RootCauseCategory(StrEnum):
    """Root-cause taxonomy; mirrors the ``category`` field of each runbook."""

    CRASHLOOP = "crashloop"
    OOM = "oom"
    IMAGE_PULL = "image_pull"
    READINESS_PROBE = "readiness_probe"
    DNS = "dns"
    UNSCHEDULABLE = "unschedulable"
    NODE_NOT_READY = "node_not_ready"
    PVC_BINDING = "pvc_binding"
    MISSING_CONFIG = "missing_config"
    CPU_THROTTLING = "cpu_throttling"
    CERT_EXPIRY = "cert_expiry"
    HPA = "hpa"
    ROLLOUT = "rollout"
    SERVICE_SELECTOR = "service_selector"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    """One observation that supports the hypothesis."""

    source: str = Field(
        description="Where it came from: logs, events, pod_spec, metrics, nodes, services, "
        "deployments or runbook:<id>"
    )
    snippet: str = Field(description="Verbatim excerpt from the tool output")


RemediationAction = Literal[
    "rollout_restart", "scale_deployment", "rollback_deployment", "patch_resources", "manual"
]


class RemediationProposal(BaseModel):
    """A remediation the supervisor recommends. Never executed without approval."""

    action: RemediationAction
    deployment: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    pending_action_id: str | None = Field(
        default=None, description="Id returned by propose_remediation, if one was filed"
    )


class RootCauseHypothesis(BaseModel):
    """The supervisor's merged conclusion."""

    summary: str
    root_cause: str
    category: RootCauseCategory
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    proposed_remediations: list[RemediationProposal] = Field(default_factory=list)
    runbook_refs: list[str] = Field(default_factory=list, description="Runbook ids cited")


class CriticVerdict(BaseModel):
    """Self-reflection result for one hypothesis."""

    verdict: Literal["PASS", "REVISE"]
    reasons: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(
        default_factory=list, description="What the supervisor should check next round"
    )
