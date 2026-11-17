"""kind cluster + fault-injection harness shared by the eval and the fixture recorder."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EVAL_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = EVAL_DIR / "scenarios"
BASE_APP = EVAL_DIR / "manifests" / "base-app.yaml"
METRICS_SERVER_URL = (
    "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml"
)


@dataclass
class Scenario:
    name: str
    description: str
    symptom: str
    fault_manifest: str
    wait_for: dict[str, Any]
    expected: dict[str, Any]
    needs_metrics_server: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return f"eval-{self.name}"


def load_scenarios(names: list[str] | None = None) -> list[Scenario]:
    out = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if names and raw["name"] not in names:
            continue
        out.append(
            Scenario(
                name=raw["name"],
                description=raw["description"],
                symptom=raw["symptom"],
                fault_manifest=raw["fault_manifest"],
                wait_for=raw["wait_for"],
                expected=raw["expected"],
                needs_metrics_server=raw.get("needs_metrics_server", False),
            )
        )
    return out


def _cmd(env_var: str, default: str) -> list[str]:
    return shlex.split(os.environ.get(env_var, default))


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    """Run kubectl (override the binary with KUBECTL, e.g. ``wsl.exe -e ~/bin/kubectl``)."""
    res = subprocess.run(
        [*_cmd("KUBECTL", "kubectl"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def kind(*args: str) -> str:
    res = subprocess.run(
        [*_cmd("KIND", "kind"), *args], capture_output=True, text=True, timeout=600, check=False
    )
    if res.returncode != 0:
        raise RuntimeError(f"kind {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def ensure_cluster(name: str = "triage-eval") -> None:
    clusters = kind("get", "clusters").split()
    if name not in clusters:
        kind("create", "cluster", "--name", name, "--wait", "120s")
    if "KUBECTL" not in os.environ:
        kubectl("config", "use-context", f"kind-{name}")


def ensure_metrics_server() -> None:
    if "metrics-server" in kubectl("get", "deploy", "-n", "kube-system", check=False):
        return
    kubectl("apply", "-f", METRICS_SERVER_URL)
    # kind kubelets use self-signed serving certs.
    kubectl(
        "patch",
        "deployment",
        "metrics-server",
        "-n",
        "kube-system",
        "--type=json",
        "-p",
        json.dumps(
            [
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/0/args/-",
                    "value": "--kubelet-insecure-tls",
                }
            ]
        ),
    )
    kubectl("rollout", "status", "deployment/metrics-server", "-n", "kube-system", "--timeout=180s")


def inject(scenario: Scenario) -> None:
    """Create the namespace, deploy the healthy app, then apply the fault."""
    ns = scenario.namespace
    kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=true")
    kubectl("create", "namespace", ns)
    kubectl("apply", "-n", ns, "-f", "-", stdin=BASE_APP.read_text(encoding="utf-8"))
    kubectl("rollout", "status", "deployment/checkout", "-n", ns, "--timeout=180s")
    kubectl("apply", "-n", ns, "-f", "-", stdin=scenario.fault_manifest)


def _pods(ns: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = json.loads(kubectl("get", "pods", "-n", ns, "-o", "json"))[
        "items"
    ]
    return items


def _symptom_present(scenario: Scenario) -> bool:
    w = scenario.wait_for
    ns = scenario.namespace
    if "pod_reason" in w:
        wanted = set(w["pod_reason"])
        min_restarts = int(w.get("min_restarts", 0))
        for pod in _pods(ns):
            for cs in pod.get("status", {}).get("containerStatuses") or []:
                reasons = {
                    (cs.get("state", {}).get(k) or {}).get("reason")
                    for k in ("waiting", "terminated")
                } | {(cs.get("lastState", {}).get("terminated") or {}).get("reason")}
                if reasons & wanted and cs.get("restartCount", 0) >= min_restarts:
                    return True
        return False
    ok = True
    if "event_reason" in w:
        evs = json.loads(kubectl("get", "events", "-n", ns, "-o", "json"))["items"]
        ok &= any(e.get("reason") == w["event_reason"] for e in evs)
    if "deployment_ready" in w:
        dep = json.loads(kubectl("get", "deploy", w["deployment_ready"], "-n", ns, "-o", "json"))
        st = dep.get("status", {})
        ok &= st.get("readyReplicas", 0) == dep["spec"]["replicas"] and not st.get(
            "unavailableReplicas"
        )
    if w.get("pod_metrics"):
        ok &= bool(kubectl("top", "pods", "-n", ns, "--no-headers", check=False).strip())
    return ok


def wait_for_symptom(scenario: Scenario) -> float:
    """Poll until the injected fault is observable. Returns seconds waited."""
    timeout = float(scenario.wait_for.get("timeout_s", 180))
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if _symptom_present(scenario):
            # Let a couple more events / restarts accumulate for realistic evidence.
            time.sleep(float(scenario.wait_for.get("settle_s", 10)))
            return time.monotonic() - start
        time.sleep(5)
    raise TimeoutError(f"{scenario.name}: symptom not observed within {timeout:.0f}s")


def cleanup(scenario: Scenario) -> None:
    kubectl("delete", "namespace", scenario.namespace, "--ignore-not-found", "--wait=false")
