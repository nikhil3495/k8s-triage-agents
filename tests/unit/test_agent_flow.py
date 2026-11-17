"""Drive the real ADK hierarchy (Supervisor -> workers -> Critic loop) with a scripted model."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import NS, POD
from tests.fakes import ScriptedLlm, triage_script
from triage.agents.factory import Models
from triage.investigations import InvestigationService
from triage.runtime import Runtime

HYP: dict[str, Any] = {
    "summary": "checkout pods cannot pull their image",
    "root_cause": "Image tag nginx:1.27.99-doesnotexist does not exist",
    "category": "image_pull",
    "confidence": 0.92,
    "evidence": [
        {"source": "events", "snippet": 'Failed to pull image "nginx:1.27.99-doesnotexist"'},
        {"source": "pod_spec", "snippet": "reason: ImagePullBackOff"},
    ],
    "proposed_remediations": [{"action": "rollback_deployment", "deployment": "checkout"}],
    "runbook_refs": ["imagepullbackoff"],
}


def _service(rt: Runtime, llm: ScriptedLlm) -> InvestigationService:
    return InvestigationService(rt, Models(supervisor=llm, worker=llm, critic=llm))


@pytest.mark.usefixtures("replay_reader")
async def test_full_hierarchy_pass(runtime: Runtime) -> None:
    llm = ScriptedLlm(
        scripts=triage_script(
            NS,
            pod=POD,
            hypothesis=HYP,
            remediation={
                "action": "rollback_deployment",
                "deployment": "checkout",
                "rationale": "previous revision used a valid tag",
            },
        )
    )
    svc = _service(runtime, llm)
    inv = await svc.create(NS, "pods not starting", scenario="synthetic-image-pull")
    report = await svc.run(inv)

    assert report["status"] == "completed", report["error"]
    assert report["hypothesis"]["category"] == "image_pull"
    assert report["critic"]["final_verdict"] == "PASS"
    assert len(report["critic"]["rounds"]) == 1
    agents_called = {a for a, _ in llm.calls}
    assert agents_called == {"Supervisor", "LogAgent", "MetricsAgent", "RunbookAgent", "Critic"}
    # The remediation was only *proposed*.
    [action] = report["pending_actions"]
    assert action["status"] == "pending"
    assert action["action"] == "rollback_deployment"
    # Telemetry: per-agent usage and cost rolled up from nested AgentTool runs.
    by_agent = report["usage"]["by_agent"]
    assert {"Supervisor", "LogAgent", "Critic"} <= set(by_agent)
    assert by_agent["LogAgent"]["tool_calls"] == 3
    assert report["cost_usd"] > 0
    tools = [s["tool"] for s in report["steps"] if s["kind"] == "tool"]
    assert "list_pods" in tools
    assert "search_runbooks" in tools
    assert report["runbook_citations"]
    checkpoint_agents = [c["agent"] for c in report["checkpoints"]]
    assert "LogAgent" in checkpoint_agents
    assert "Critic" in checkpoint_agents


@pytest.mark.usefixtures("replay_reader")
async def test_critic_revise_triggers_second_round(runtime: Runtime) -> None:
    script = triage_script(
        NS,
        pod=POD,
        hypothesis=HYP,
        critic=[
            {"verdict": "REVISE", "reasons": ["no event evidence"], "missing_evidence": ["events"]},
            {"verdict": "PASS", "reasons": ["now supported"]},
        ],
    )
    llm = ScriptedLlm(scripts=script)
    svc = _service(runtime, llm)
    inv = await svc.create(NS, "pods not starting", scenario="synthetic-image-pull")
    report = await svc.run(inv)

    assert report["status"] == "completed", report["error"]
    rounds = report["critic"]["rounds"]
    assert [r["verdict"] for r in rounds] == ["REVISE", "PASS"]
    assert sum(1 for a, i in llm.calls if a == "Supervisor" and i == 0) == 2
