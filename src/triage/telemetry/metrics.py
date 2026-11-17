"""OpenTelemetry metrics (exported to Cloud Monitoring in prod) + an in-process summary.

The in-process aggregate backs ``GET /metrics/summary`` so the numbers are visible
without a metrics backend; the OTel instruments carry the same data to Cloud
Monitoring / any OTLP collector.
"""

from __future__ import annotations

import statistics
import threading
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics

if TYPE_CHECKING:
    from triage.hitl.models import PendingAction

_meter = metrics.get_meter("triage")

request_latency = _meter.create_histogram(
    "triage.request.latency", unit="ms", description="End-to-end investigation latency"
)
tokens_per_sec = _meter.create_histogram(
    "triage.llm.tokens_per_sec", unit="{token}/s", description="Output tokens/sec per LLM call"
)
request_cost = _meter.create_histogram(
    "triage.request.cost", unit="USD", description="LLM cost per investigation"
)
critic_verdicts = _meter.create_counter(
    "triage.critic.verdicts", description="Critic verdicts by outcome (PASS / REVISE)"
)
approval_wait = _meter.create_histogram(
    "triage.approval.wait", unit="s", description="Time a remediation waited for a human decision"
)

_WINDOW = 500


class _Summary:
    """Thread-safe rolling aggregates for the summary endpoint."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.latency_ms: deque[float] = deque(maxlen=_WINDOW)
        self.cost_usd: deque[float] = deque(maxlen=_WINDOW)
        self.tps: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_WINDOW))
        self.verdicts: dict[str, int] = defaultdict(int)
        self.approval_wait_s: deque[float] = deque(maxlen=_WINDOW)
        self.decisions: dict[str, int] = defaultdict(int)


_summary = _Summary()


def record_request(latency_ms: float, cost: float, status: str) -> None:
    """Record one finished investigation."""
    attrs = {"status": status}
    request_latency.record(latency_ms, attrs)
    request_cost.record(cost, attrs)
    with _summary.lock:
        _summary.latency_ms.append(latency_ms)
        _summary.cost_usd.append(cost)


def record_tokens_per_sec(agent: str, model: str, tps: float) -> None:
    """Record output tokens/sec for one LLM call."""
    tokens_per_sec.record(tps, {"agent.name": agent, "llm.model": model})
    with _summary.lock:
        _summary.tps[agent].append(tps)


def record_critic_verdict(verdict: str) -> None:
    """Count a critic verdict."""
    critic_verdicts.add(1, {"verdict": verdict})
    with _summary.lock:
        _summary.verdicts[verdict] += 1


def record_approval_decision(action: PendingAction, wait_s: float) -> None:
    """HITL hook: approval wait time and decision outcome."""
    approval_wait.record(wait_s, {"action": str(action.action), "status": str(action.status)})
    with _summary.lock:
        _summary.approval_wait_s.append(wait_s)
        _summary.decisions[str(action.status)] += 1


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 6),
        "p50": round(statistics.median(values), 6),
        "p95": round(p95, 6),
    }


def summary() -> dict[str, Any]:
    """Snapshot of all aggregates (rolling window of the last 500 samples)."""
    with _summary.lock:
        total = sum(_summary.verdicts.values())
        return {
            "request_latency_ms": _stats(list(_summary.latency_ms)),
            "cost_per_request_usd": _stats(list(_summary.cost_usd)),
            "tokens_per_sec_by_agent": {a: _stats(list(v)) for a, v in _summary.tps.items()},
            "critic": {
                "verdicts": dict(_summary.verdicts),
                "rejection_rate": round(_summary.verdicts.get("REVISE", 0) / total, 4)
                if total
                else None,
            },
            "approvals": {
                "wait_seconds": _stats(list(_summary.approval_wait_s)),
                "decisions": dict(_summary.decisions),
            },
        }


def reset_summary() -> None:
    """Clear aggregates (tests)."""
    with _summary.lock:
        _summary.reset()
