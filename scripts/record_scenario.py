"""Inject each eval fault on kind and record a replay fixture from the live API.

Usage:
    uv run python -m scripts.record_scenario                # all scenarios
    uv run python -m scripts.record_scenario oom-killed     # one scenario

Env: KUBECTL / KIND override the binaries (e.g. through WSL), KUBECONFIG /
KUBE_CONTEXT select the cluster for the Python client.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from eval import harness

from triage.k8s.live import LiveClusterClient
from triage.k8s.snapshot import capture_snapshot

FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "fixtures"


def record(names: list[str] | None, *, keep: bool = False) -> list[Path]:
    harness.ensure_cluster()
    scenarios = harness.load_scenarios(names)
    if any(s.needs_metrics_server for s in scenarios):
        harness.ensure_metrics_server()
    client = LiveClusterClient(context=os.environ.get("KUBE_CONTEXT", "kind-triage-eval"))
    written = []
    for sc in scenarios:
        print(f"[record] {sc.name}: injecting fault in {sc.namespace}", flush=True)
        harness.inject(sc)
        waited = harness.wait_for_symptom(sc)
        snap = capture_snapshot(client, sc.name, sc.namespace)
        snap["symptom"] = sc.symptom
        path = FIXTURES / f"{sc.name}.json"
        path.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"[record] {sc.name}: symptom after {waited:.0f}s -> {path.name}", flush=True)
        written.append(path)
        if not keep:
            harness.cleanup(sc)
    return written


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    record(args or None, keep="--keep" in sys.argv)
