from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import NS, POD
from triage import state_keys
from triage.config import Settings, TraceExporter
from triage.k8s.base import ClusterError
from triage.k8s.replay import ReplayClusterClient
from triage.runtime import Runtime
from triage.sessions.checkpoints import make_tool_budget, record_checkpoint, record_critic_verdict
from triage.telemetry import metrics, pricing
from triage.telemetry.plugin import InvocationUsage
from triage.tools import cluster_tools as ct
from triage.tools.registry import ReaderRegistry
from triage.tools.remediation_tools import propose_remediation
from triage.tools.runbook_tools import search_runbooks


class Ctx:
    """Minimal stand-in for ADK's ToolContext / CallbackContext."""

    def __init__(self, state: dict[str, Any] | None = None, agent: str = "LogAgent") -> None:
        self.state: dict[str, Any] = state or {state_keys.NAMESPACE: NS}
        self.agent_name = agent
        self.invocation_id = "inv-1"


@pytest.mark.usefixtures("replay_reader")
def test_cluster_tools() -> None:
    ctx: Any = Ctx()
    assert ct.list_pods(NS, ctx)["pods"][0]["reason"] == "ImagePullBackOff"
    assert ct.describe_pod(NS, POD, ctx)["pod"]["name"] == POD
    assert ct.describe_pod(NS, "missing", ctx) == {"error": 'pods "missing" not found'}
    assert ct.get_pod_logs(NS, POD, False, ctx)["truncated"] is False
    events = ct.get_events(NS, "", ctx)["events"]
    assert events[0]["type"] == "Warning"
    svcs = ct.list_services(NS, ctx)["services"]
    assert svcs[0]["pods_matching_selector"] == 1
    assert ct.list_deployments(NS, ctx)["deployments"][0]["replicas"] == 2
    assert ct.get_restart_counts(NS, ctx)["containers"][0]["restarts"] == 0
    usage = ct.get_pod_resource_usage(NS, ctx)
    row = usage["containers"][0]
    assert row["cpu_pct_of_limit"] == 95.0
    assert row["memory_pct_of_limit"] == pytest.approx(15.6, abs=0.1)
    assert ct.get_node_status(ctx)["nodes"][0]["usage"]["cpu_m"] == 800.0
    assert "error" in ct.list_pods("wrong-ns", ctx)


def test_resource_usage_without_metrics(snapshot: dict[str, Any]) -> None:
    class NoMetrics(ReplayClusterClient):
        def get_pod_metrics(self, namespace: str) -> list[dict[str, Any]]:
            raise ClusterError("metrics-server unavailable: NotFound")

        def get_node_metrics(self) -> list[dict[str, Any]]:
            raise ClusterError("metrics-server unavailable: NotFound")

    from triage.tools.registry import registry

    registry.set_factory(lambda _m, _s: NoMetrics(snapshot))
    ctx: Any = Ctx()
    assert "hint" in ct.get_pod_resource_usage(NS, ctx)
    assert ct.get_node_status(ctx)["nodes"][0]["usage"] is None


def test_registry_caches_per_scenario(snapshot: dict[str, Any]) -> None:
    built: list[tuple[str, Any]] = []

    def factory(mode: str, scenario: Any) -> Any:
        built.append((mode, scenario))
        return ReplayClusterClient(snapshot)

    reg = ReaderRegistry(factory)
    a = reg.for_state({state_keys.CLUSTER_MODE: "replay", state_keys.SCENARIO: "a"})
    b = reg.for_state({state_keys.CLUSTER_MODE: "replay", state_keys.SCENARIO: "a"})
    reg.for_state({state_keys.CLUSTER_MODE: "replay", state_keys.SCENARIO: "b"})
    assert a is b
    assert built == [("replay", "a"), ("replay", "b")]


def test_default_registry_factory_loads_fixture(tmp_path: Any, snapshot: dict[str, Any]) -> None:
    from tests.conftest import write_snapshot
    from triage.config import get_settings
    from triage.tools import registry as regmod

    write_snapshot(tmp_path, snapshot)
    get_settings().replay_fixtures_dir = tmp_path
    reader = regmod._default_factory("replay", "synthetic-image-pull")
    assert reader.list_pods(NS)


@pytest.mark.usefixtures("replay_reader")
def test_propose_and_search_tools(runtime: Runtime) -> None:
    ctx: Any = Ctx({state_keys.NAMESPACE: NS, "investigation_id": "inv_1"}, agent="Supervisor")
    out = propose_remediation(
        "patch_resources", "checkout", "raise memory", ctx, container="web", memory_limit="256Mi"
    )
    assert out["status"] == "pending_human_approval"
    assert "128Mi -> 256Mi" in out["preview"]
    assert ctx.state[state_keys.PROPOSED_ACTIONS] == [out["pending_action_id"]]
    bad = propose_remediation("scale_deployment", "checkout", "x", ctx, replicas=999)
    assert "error" in bad
    res = search_runbooks("ImagePullBackOff", ctx)
    assert res["retrieval_mode"] == "bm25"
    assert res["results"][0]["runbook_id"] == "imagepullbackoff"
    assert ctx.state["runbook_citations"]


def test_tool_budget_and_checkpoints() -> None:
    ctx: Any = Ctx()
    budget = make_tool_budget(2)
    assert budget(None, {}, ctx) is None
    assert budget(None, {}, ctx) is None
    assert "budget" in budget(None, {}, ctx)["error"]
    record_checkpoint(ctx)
    assert ctx.state[state_keys.CHECKPOINTS][0]["agent"] == "LogAgent"
    ctx.state[state_keys.CRITIC_VERDICT] = {"verdict": "REVISE", "reasons": ["weak"]}
    record_critic_verdict(ctx)
    assert ctx.state[state_keys.CRITIC_HISTORY] == [
        {"round": 1, "verdict": "REVISE", "reasons": ["weak"]}
    ]


def test_pricing() -> None:
    assert pricing.cost_usd("gemini-3.1-flash-lite", 1_000_000, 1_000_000) == pytest.approx(1.75)
    assert pricing.cost_usd("models/gemini-3.8-flash", 1000, 100) == pytest.approx(0.0011250)
    assert pricing.price_for("gemini-3.1-flash-lite-001") == pricing.PRICES["gemini-3.1-flash-lite"]
    assert pricing.cost_usd("unknown-model", 10, 10) == 0.0
    assert pricing.price_for(None) is None


def test_pricing_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PRICES_JSON", '{"my-model": {"input": 1, "output": 2}}')
    prices = pricing._load_prices()
    assert prices["my-model"] == pricing.Price(1.0, 2.0)


def test_metrics_summary() -> None:
    metrics.reset_summary()
    assert metrics.summary()["critic"]["rejection_rate"] is None
    metrics.record_request(1200.0, 0.01, "completed")
    metrics.record_request(800.0, 0.03, "completed")
    metrics.record_tokens_per_sec("LogAgent", "m", 50.0)
    metrics.record_critic_verdict("PASS")
    metrics.record_critic_verdict("REVISE")
    s = metrics.summary()
    assert s["request_latency_ms"]["p50"] == 1000.0
    assert s["cost_per_request_usd"]["mean"] == pytest.approx(0.02)
    assert s["critic"]["rejection_rate"] == 0.5
    assert s["tokens_per_sec_by_agent"]["LogAgent"]["count"] == 1


def test_usage_ledger() -> None:
    a, b = InvocationUsage(), InvocationUsage()
    a.agent("X").output_tokens = 100
    a.agent("X").llm_latency_ms = 2000
    b.agent("X").output_tokens = 50
    b.agent("Y").cost_usd = 0.5
    a.merge(b)
    d = a.to_dict()
    assert d["by_agent"]["X"]["output_tokens"] == 150
    assert d["by_agent"]["X"]["tokens_per_sec"] == 75.0
    assert d["totals"]["cost_usd"] == 0.5
    assert InvocationUsage().agent("Z").tokens_per_sec is None


def test_telemetry_setup_is_idempotent() -> None:
    from triage.telemetry import setup

    setup._installed = False
    s = Settings(_env_file=None, trace_exporter=TraceExporter.CONSOLE)  # type: ignore[call-arg]
    setup.setup_telemetry(s)
    setup.setup_telemetry(s)
    assert setup._installed
    assert setup._span_exporter(Settings(_env_file=None, trace_exporter=TraceExporter.NONE)) is None  # type: ignore[call-arg]
    otlp = setup._span_exporter(Settings(_env_file=None, trace_exporter=TraceExporter.OTLP))  # type: ignore[call-arg]
    assert type(otlp).__name__ == "OTLPSpanExporter"
