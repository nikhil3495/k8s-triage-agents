"""Score one investigation report against a scenario's expectations."""

from __future__ import annotations

import json
import statistics
from typing import Any


def _hyp_text(hyp: dict[str, Any]) -> str:
    parts = [hyp.get("summary", ""), hyp.get("root_cause", "")]
    parts += [e.get("snippet", "") for e in hyp.get("evidence", [])]
    return "\n".join(parts).lower()


def score(report: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Root-cause accuracy, evidence recall, runbook hit@3, critic agreement, latency, cost."""
    hyp = report.get("hypothesis") or {}
    text = _hyp_text(hyp)
    keywords = [str(k).lower() for k in expected.get("evidence_keywords", [])]
    hits = [k for k in keywords if k in text or k in json.dumps(hyp).lower()]
    correct = hyp.get("category") == expected["category"]
    refs = list(hyp.get("runbook_refs") or [])
    cited_runbooks: list[str] = []
    for c in report.get("runbook_citations") or []:
        rb = c.split("#", 1)[0]
        if rb not in cited_runbooks:
            cited_runbooks.append(rb)
    top3 = (refs or cited_runbooks)[:3]
    verdict = report.get("critic", {}).get("final_verdict")
    return {
        "status": report.get("status"),
        "predicted_category": hyp.get("category"),
        "expected_category": expected["category"],
        "root_cause_correct": correct,
        "evidence_recall": round(len(hits) / len(keywords), 3) if keywords else 1.0,
        "evidence_missing": [k for k in keywords if k not in hits],
        "runbook_hit_at_3": expected["runbook"] in top3,
        "runbooks_top3": top3,
        "critic_verdict": verdict,
        "critic_rounds": len(report.get("critic", {}).get("rounds", [])),
        "critic_agreement": (verdict == "PASS") == correct,
        "latency_ms": report.get("latency_ms"),
        "cost_usd": report.get("cost_usd", 0.0),
        "confidence": hyp.get("confidence"),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean metrics over all scored scenarios."""
    if not rows:
        return {"scenarios": 0}

    def mean(key: str) -> float:
        return round(statistics.fmean(float(r[key]) for r in rows), 3)

    lat = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    return {
        "scenarios": len(rows),
        "root_cause_accuracy": mean("root_cause_correct"),
        "evidence_recall": mean("evidence_recall"),
        "runbook_hit_at_3": mean("runbook_hit_at_3"),
        "critic_agreement": mean("critic_agreement"),
        "latency_ms_p50": round(statistics.median(lat), 1) if lat else None,
        "latency_ms_max": round(max(lat), 1) if lat else None,
        "cost_usd_total": round(sum(r["cost_usd"] for r in rows), 6),
        "cost_usd_mean": round(statistics.fmean(r["cost_usd"] for r in rows), 6),
    }
