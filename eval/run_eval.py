"""Evaluation suite / release gate.

Examples::

    # Offline (every PR): recorded fixtures + rule-based baseline model, no API key.
    python -m eval.run_eval --mode replay --model baseline

    # Recorded fixtures + Gemini.
    python -m eval.run_eval --mode replay --model gemini

    # Release gate: kind cluster, inject each fault, investigate live with Gemini,
    # and refresh the replay fixtures from the same run.
    python -m eval.run_eval --mode live --model gemini --record --threshold 0.75
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval import harness
from eval.scoring import aggregate, score
from triage.agents.factory import Models
from triage.config import ClusterMode, Settings, TraceExporter
from triage.investigations import InvestigationService
from triage.k8s.base import ClusterReader
from triage.k8s.replay import ReplayClusterClient
from triage.runtime import build_runtime, set_runtime
from triage.tools.registry import registry

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES = EVAL_DIR / "fixtures"
RESULTS = EVAL_DIR / "results"


def _models(kind: str, settings: Settings) -> Models | None:
    if kind == "gemini":
        if not (settings.gemini_api_key or os.environ.get("GOOGLE_API_KEY")):
            sys.exit("GEMINI_API_KEY is required for --model gemini")
        if settings.gemini_api_key:
            os.environ.setdefault("GOOGLE_API_KEY", settings.gemini_api_key.get_secret_value())
        return None  # use SUPERVISOR_MODEL / WORKER_MODEL / CRITIC_MODEL
    if kind in ("baseline", "scripted"):
        from eval.baseline import BaselineLlm

        llm = BaselineLlm()
        return Models(supervisor=llm, worker=llm, critic=llm)
    sys.exit(f"unknown model kind {kind}")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    workdir = Path(tempfile.mkdtemp(prefix="triage-eval-"))
    settings = Settings(
        cluster_mode=ClusterMode(args.mode),
        session_db_url=f"sqlite+aiosqlite:///{(workdir / 'sessions.db').as_posix()}",
        app_db_url=f"sqlite:///{(workdir / 'app.db').as_posix()}",
        trace_exporter=TraceExporter.NONE,
        replay_fixtures_dir=FIXTURES,
    )
    rt = build_runtime(settings)
    set_runtime(rt)
    svc = InvestigationService(rt, _models(args.model, settings))
    scenarios = harness.load_scenarios(args.scenarios or None)
    if args.mode == "live":
        harness.ensure_cluster()
        if any(s.needs_metrics_server for s in scenarios):
            harness.ensure_metrics_server()

    rows: list[dict[str, Any]] = []
    for sc in scenarios:
        print(f"[eval] {sc.name}", flush=True)
        reader: ClusterReader
        if args.mode == "live":
            from triage.k8s.live import LiveClusterClient
            from triage.k8s.snapshot import capture_snapshot

            harness.inject(sc)
            harness.wait_for_symptom(sc)
            reader = LiveClusterClient(context=os.environ.get("KUBE_CONTEXT", "kind-triage-eval"))
            if args.record:
                snap = capture_snapshot(reader, sc.name, sc.namespace)
                snap["symptom"] = sc.symptom
                (FIXTURES / f"{sc.name}.json").write_text(
                    json.dumps(snap, indent=1, sort_keys=True) + "\n", encoding="utf-8"
                )
        else:
            path = FIXTURES / f"{sc.name}.json"
            if not path.is_file():
                print(f"[eval]   no fixture for {sc.name}; skipped (pending)", flush=True)
                rows.append({"scenario": sc.name, "status": "pending"})
                continue
            reader = ReplayClusterClient.from_scenario(FIXTURES, sc.name)
        registry.set_factory(lambda _m, _s, r=reader: r)  # type: ignore[misc]
        t0 = time.perf_counter()
        inv = await svc.create(sc.namespace, sc.symptom, scenario=sc.name)
        report = await svc.run(inv)
        row = {"scenario": sc.name, **score(report, sc.expected)}
        row["wall_s"] = round(time.perf_counter() - t0, 2)
        rows.append(row)
        print(
            f"[eval]   {row['predicted_category']} (expected {row['expected_category']}) "
            f"recall={row['evidence_recall']} hit@3={row['runbook_hit_at_3']} "
            f"critic={row['critic_verdict']} cost=${row['cost_usd']:.5f}",
            flush=True,
        )
        if args.mode == "live" and not args.keep:
            harness.cleanup(sc)

    scored = [r for r in rows if r.get("status") != "pending"]
    summary = aggregate(scored)
    result = {
        "run_at": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "model": args.model,
        "models": {
            "supervisor": settings.supervisor_model if args.model == "gemini" else args.model,
            "worker": settings.worker_model if args.model == "gemini" else args.model,
            "critic": settings.critic_model if args.model == "gemini" else args.model,
        },
        "retrieval_mode": rt.retriever.mode,
        "threshold": args.threshold,
        "summary": summary,
        "passed": bool(scored) and summary["root_cause_accuracy"] >= args.threshold,
        "scenarios": rows,
    }
    set_runtime(None)
    return result


def to_markdown(result: dict[str, Any]) -> str:
    s = result["summary"]
    lines = [
        f"# Eval report - {result['mode']} / {result['model']}",
        "",
        f"Run at {result['run_at']} · retrieval `{result['retrieval_mode']}` · "
        f"gate: root-cause accuracy >= {result['threshold']} -> "
        f"**{'PASS' if result['passed'] else 'FAIL'}**",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    for k in (
        "scenarios",
        "root_cause_accuracy",
        "evidence_recall",
        "runbook_hit_at_3",
        "critic_agreement",
        "latency_ms_p50",
        "cost_usd_mean",
        "cost_usd_total",
    ):
        lines.append(f"| {k} | {s.get(k)} |")
    lines += [
        "",
        "| Scenario | Predicted | Expected | Evidence recall | Runbook hit@3 | Critic "
        "| Latency ms | Cost $ |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in result["scenarios"]:
        if r.get("status") == "pending":
            lines.append(f"| {r['scenario']} | pending | | | | | | |")
            continue
        lines.append(
            f"| {r['scenario']} | {r['predicted_category']} | {r['expected_category']} | "
            f"{r['evidence_recall']} | {r['runbook_hit_at_3']} | {r['critic_verdict']} | "
            f"{round(r['latency_ms'] or 0)} | {r['cost_usd']:.5f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mode", choices=["replay", "live"], default="replay")
    ap.add_argument("--model", choices=["baseline", "scripted", "gemini"], default="baseline")
    ap.add_argument("--scenarios", nargs="*")
    ap.add_argument("--threshold", type=float, default=0.75)
    ap.add_argument("--record", action="store_true", help="live mode: refresh replay fixtures")
    ap.add_argument("--keep", action="store_true", help="live mode: keep namespaces")
    ap.add_argument("--out", type=Path, default=RESULTS / "latest.json")
    args = ap.parse_args()
    result = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    args.out.with_suffix(".md").write_text(to_markdown(result), encoding="utf-8")
    print(to_markdown(result))
    if not result["passed"]:
        sys.exit(
            f"eval gate failed: accuracy {result['summary'].get('root_cause_accuracy')} "
            f"< {args.threshold}"
        )


if __name__ == "__main__":
    main()
