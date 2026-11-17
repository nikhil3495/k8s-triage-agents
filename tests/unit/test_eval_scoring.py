from __future__ import annotations

from eval.harness import load_scenarios
from eval.scoring import aggregate, score

from triage.agents.baseline import classify

EXPECTED = {
    "category": "oom",
    "evidence_keywords": ["OOMKilled", "137", "48Mi"],
    "runbook": "oomkilled",
}


def _report(category: str, verdict: str = "PASS") -> dict:
    return {
        "status": "completed",
        "hypothesis": {
            "summary": "s",
            "root_cause": "container OOMKilled with exit code 137",
            "category": category,
            "evidence": [{"source": "pod_spec", "snippet": "limits.memory: 48Mi"}],
            "runbook_refs": ["oomkilled", "crashloopbackoff"],
        },
        "critic": {"final_verdict": verdict, "rounds": [{"verdict": verdict}]},
        "runbook_citations": [],
        "latency_ms": 1000.0,
        "cost_usd": 0.002,
    }


def test_score_correct() -> None:
    row = score(_report("oom"), EXPECTED)
    assert row["root_cause_correct"]
    assert row["evidence_recall"] == 1.0
    assert row["runbook_hit_at_3"]
    assert row["critic_agreement"]


def test_score_wrong_and_aggregate() -> None:
    bad = score(_report("crashloop", verdict="PASS"), EXPECTED)
    assert not bad["root_cause_correct"]
    assert not bad["critic_agreement"]  # critic passed a wrong answer
    good = score(_report("oom"), EXPECTED)
    agg = aggregate([good, bad])
    assert agg["root_cause_accuracy"] == 0.5
    assert agg["critic_agreement"] == 0.5
    assert agg["cost_usd_total"] == 0.004
    assert aggregate([]) == {"scenarios": 0}


def test_citations_used_when_no_refs() -> None:
    rep = _report("oom")
    rep["hypothesis"]["runbook_refs"] = []
    rep["runbook_citations"] = ["oomkilled#symptoms", "oomkilled#diagnosis", "dns-failure#summary"]
    assert score(rep, EXPECTED)["runbooks_top3"] == ["oomkilled", "dns-failure"]


def test_scenarios_are_well_formed() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) >= 8
    for s in scenarios:
        assert s.namespace == f"eval-{s.name}"
        assert {"category", "evidence_keywords", "runbook"} <= set(s.expected)
        assert "kind: Deployment" in s.fault_manifest


def test_baseline_classifier_rules() -> None:
    assert classify('"reason": "ImagePullBackOff"')[0] == "image_pull"
    assert classify('"reason": "OOMKilled"')[0] == "oom"
    assert classify('"cpu_pct_of_limit": 99.5')[0] == "cpu_throttling"
    assert classify('"ready_endpoints": 0, "pods_matching_selector": 0')[0] == "service_selector"
    assert classify("all good")[0] == "unknown"
