"""Integration: the real agent graph over fixtures recorded from kind."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from eval.harness import load_scenarios
from eval.scoring import score

from triage.agents.baseline import BaselineLlm
from triage.agents.factory import Models
from triage.config import REPO_ROOT
from triage.investigations import InvestigationService
from triage.k8s.replay import ReplayClusterClient, list_scenarios
from triage.runtime import Runtime
from triage.tools.registry import registry

FIXTURES = REPO_ROOT / "eval" / "fixtures"
SCENARIOS = {s.name: s for s in load_scenarios()}

pytestmark = pytest.mark.integration


def test_every_scenario_has_a_recorded_fixture() -> None:
    assert set(list_scenarios(FIXTURES)) == set(SCENARIOS)
    for name in SCENARIOS:
        r = ReplayClusterClient.from_scenario(FIXTURES, name)
        assert r.snapshot["source"] == "live-kind"
        assert r.namespace == f"eval-{name}"
        assert r.list_pods(r.namespace), name


@pytest.fixture
def fixture_reader(request: Any) -> Iterator[None]:
    yield
    registry.set_factory(lambda _m, _s: (_ for _ in ()).throw(AssertionError("unset")))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_pipeline_on_recorded_fixture(
    name: str, runtime: Runtime, fixture_reader: None
) -> None:
    sc = SCENARIOS[name]
    reader = ReplayClusterClient.from_scenario(FIXTURES, name)
    registry.set_factory(lambda _m, _s: reader)
    llm = BaselineLlm()
    svc = InvestigationService(runtime, Models(llm, llm, llm))
    inv = await svc.create(sc.namespace, sc.symptom, scenario=name)
    report = await svc.run(inv)
    assert report["status"] == "completed", report["error"]
    row = score(report, sc.expected)
    assert row["root_cause_correct"], row
    assert row["runbook_hit_at_3"], row
