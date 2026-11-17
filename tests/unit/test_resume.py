"""Kill an investigation mid-run and resume it from the persisted checkpoint."""

from __future__ import annotations

import pytest

from tests.conftest import NS, POD
from tests.fakes import ScriptedLlm, triage_script
from tests.unit.test_agent_flow import HYP
from triage.agents.factory import Models
from triage.investigations import InvestigationService
from triage.runtime import Runtime


@pytest.mark.usefixtures("replay_reader")
async def test_crash_then_resume_continues_from_checkpoint(runtime: Runtime) -> None:
    llm = ScriptedLlm(
        scripts=triage_script(NS, pod=POD, hypothesis=HYP),
        # Crash the Supervisor right after LogAgent has finished (supervisor step 1 =
        # "call MetricsAgent"), i.e. mid-investigation.
        crash_at={("Supervisor", 1)},
    )
    models = Models(supervisor=llm, worker=llm, critic=llm)
    svc = InvestigationService(runtime, models)
    inv = await svc.create(NS, "pods not starting", scenario="synthetic-image-pull")

    first = await svc.run(inv)
    assert first["status"] == "failed"
    assert "simulated crash" in first["error"]
    assert [c["agent"] for c in first["checkpoints"]] == ["LogAgent"]
    log_agent_calls = sum(1 for a, _ in llm.calls if a == "LogAgent")
    assert log_agent_calls == 4  # 3 tool calls + final answer

    # Simulate a process restart: brand-new service/runner over the same durable DBs.
    svc2 = InvestigationService(runtime, models)
    resumed = await svc2.run(inv, resume=True)

    assert resumed["status"] == "completed", resumed["error"]
    assert resumed["hypothesis"]["category"] == "image_pull"
    assert resumed["critic"]["final_verdict"] == "PASS"
    # LogAgent's completed work was NOT redone after the resume.
    assert sum(1 for a, _ in llm.calls if a == "LogAgent") == log_agent_calls
    agents = [c["agent"] for c in resumed["checkpoints"]]
    assert agents.count("LogAgent") == 1
    assert "MetricsAgent" in agents
    assert "Critic" in agents


@pytest.mark.usefixtures("replay_reader")
async def test_resume_of_completed_investigation_is_noop(runtime: Runtime) -> None:
    llm = ScriptedLlm(scripts=triage_script(NS, pod=POD, hypothesis=HYP))
    svc = InvestigationService(runtime, Models(llm, llm, llm))
    inv = await svc.create(NS, "x", scenario="synthetic-image-pull")
    await svc.run(inv)
    n = len(llm.calls)
    again = await svc.run(inv, resume=True)
    assert again["status"] == "completed"
    assert len(llm.calls) == n


async def test_run_twice_without_resume_rejected(runtime: Runtime) -> None:
    llm = ScriptedLlm(scripts={})
    svc = InvestigationService(runtime, Models(llm, llm, llm))
    inv = await svc.create(NS, "x")
    svc._update(inv, status="failed")
    with pytest.raises(ValueError, match="use resume"):
        await svc.run(inv)
