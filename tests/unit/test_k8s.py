from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.conftest import NS, POD, write_snapshot
from triage.k8s import normalize
from triage.k8s.base import ClusterError, ClusterReader, ClusterWriter
from triage.k8s.replay import (
    ReplayClusterClient,
    ReplayClusterWriter,
    list_scenarios,
    load_snapshot,
)
from triage.k8s.snapshot import capture_snapshot

RAW_POD: dict[str, Any] = {
    "metadata": {
        "name": "web-1",
        "namespace": "shop",
        "labels": {"app": "web"},
        "ownerReferences": [{"name": "web-rs"}],
    },
    "spec": {
        "nodeName": "n1",
        "containers": [
            {
                "name": "web",
                "image": "busybox",
                "command": ["sh"],
                "resources": {"limits": {"memory": "48Mi"}},
                "env": [
                    {"name": "A", "value": "secret-value"},
                    {"name": "B", "valueFrom": {"x": 1}},
                ],
            }
        ],
        "volumes": [{"name": "data"}],
    },
    "status": {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
        "containerStatuses": [
            {
                "name": "web",
                "ready": False,
                "restartCount": 5,
                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }
        ],
    },
}


def test_pod_summary_and_detail() -> None:
    s = normalize.pod_summary(RAW_POD)
    assert s == {
        "name": "web-1",
        "phase": "Running",
        "ready": "0/1",
        "restarts": 5,
        "reason": "CrashLoopBackOff",
        "node": "n1",
        "owner": "web-rs",
        "labels": {"app": "web"},
    }
    d = normalize.pod_detail(RAW_POD)
    c = d["containers"][0]
    assert c["last_state"] == {"state": "terminated", "reason": "OOMKilled", "exit_code": 137}
    assert c["env"] == [{"name": "A"}, {"name": "B", "valueFrom": {"x": 1}}]  # values never leak
    assert d["volumes"] == ["data"]
    assert normalize._container_state(None) == {"state": "unknown"}
    assert normalize._container_state({"running": None}) == {"state": "unknown"}


def test_event_deployment_service_node_summaries() -> None:
    ev = normalize.event_summary(
        {
            "type": "Warning",
            "reason": "BackOff",
            "involvedObject": {"kind": "Pod", "name": "p"},
            "message": "m",
            "lastTimestamp": "t",
        }
    )
    assert ev == {
        "type": "Warning",
        "reason": "BackOff",
        "object": "Pod/p",
        "message": "m",
        "count": 1,
        "last_seen": "t",
    }
    dep = normalize.deployment_summary(
        {
            "metadata": {"name": "d", "annotations": {"deployment.kubernetes.io/revision": "3"}},
            "spec": {
                "replicas": 2,
                "selector": {"matchLabels": {"app": "d"}},
                "template": {"spec": {"containers": [{"name": "c", "image": "i"}]}},
            },
            "status": {
                "readyReplicas": 1,
                "conditions": [{"type": "Available", "status": "False"}],
            },
        }
    )
    assert dep["revision"] == "3"
    assert dep["ready_replicas"] == 1
    svc = normalize.service_summary(
        {"metadata": {"name": "s"}, "spec": {"selector": {"app": "x"}, "ports": [{"port": 80}]}},
        {"subsets": [{"addresses": [{}, {}], "notReadyAddresses": [{}]}]},
    )
    assert (svc["ready_endpoints"], svc["not_ready_endpoints"]) == (2, 1)
    assert (
        normalize.service_summary({"metadata": {"name": "s"}, "spec": {}}, None)["ready_endpoints"]
        == 0
    )
    node = normalize.node_summary(
        {
            "metadata": {"name": "n"},
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "False", "reason": "KubeletNotReady"},
                    {"type": "MemoryPressure", "status": "True"},
                ]
            },
        }
    )
    assert node["ready"] == "False"
    assert node["pressure"] == ["MemoryPressure"]


@pytest.mark.parametrize(
    ("q", "m"), [("250m", 250), ("1", 1000), ("1500000n", 1.5), ("2000u", 2), ("0.5", 500)]
)
def test_parse_cpu(q: str, m: float) -> None:
    assert normalize.parse_cpu_millicores(q) == pytest.approx(m)


@pytest.mark.parametrize(
    ("q", "mib"), [("128Mi", 128), ("1Gi", 1024), ("1024Ki", 1), ("1048576", 1), ("1M", 0.9537)]
)
def test_parse_memory(q: str, mib: float) -> None:
    assert normalize.parse_memory_mib(q) == pytest.approx(mib, rel=1e-3)


def test_metrics_summaries() -> None:
    pm = normalize.pod_metrics_summary(
        {
            "metadata": {"name": "p"},
            "containers": [{"name": "c", "usage": {"cpu": "49876543n", "memory": "20Mi"}}],
        }
    )
    assert pm["containers"][0] == {"name": "c", "cpu_m": 49.9, "memory_mib": 20.0}
    nm = normalize.node_metrics_summary(
        {"metadata": {"name": "n"}, "usage": {"cpu": "1", "memory": "1Gi"}}
    )
    assert nm == {"node": "n", "cpu_m": 1000.0, "memory_mib": 1024.0}


def test_replay_client(snapshot: dict[str, Any], tmp_path: Path) -> None:
    write_snapshot(tmp_path, snapshot)
    assert list_scenarios(tmp_path) == ["synthetic-image-pull"]
    r = ReplayClusterClient.from_scenario(tmp_path, "synthetic-image-pull")
    assert isinstance(r, ClusterReader)
    assert r.list_pods(NS)[0]["name"] == POD
    assert r.describe_pod(NS, POD)["containers"][0]["image"].endswith("doesnotexist")
    assert r.get_pod_logs(NS, POD) == ""
    assert len(r.get_events(NS, POD)) == 2
    assert r.get_events(NS, "other") == []
    assert r.list_deployments(NS)[0]["name"] == "checkout"
    assert r.list_services(NS)[0]["ready_endpoints"] == 0
    assert r.list_nodes()[0]["ready"] == "True"
    assert r.get_pod_metrics(NS)[0]["pod"] == POD
    assert r.get_node_metrics()[0]["node"] == "kind-control-plane"
    with pytest.raises(ClusterError, match="not in snapshot"):
        r.list_pods("default")
    with pytest.raises(ClusterError, match="not found"):
        r.describe_pod(NS, "nope")
    with pytest.raises(ClusterError, match="not found"):
        r.get_pod_logs(NS, "nope")
    with pytest.raises(ClusterError, match="unknown scenario"):
        ReplayClusterClient.from_scenario(tmp_path, "nope")
    bad = tmp_path / "bad.json"
    bad.write_text('{"scenario": "x"}')
    with pytest.raises(ClusterError, match="missing keys"):
        load_snapshot(bad)


def test_snapshot_roundtrip(snapshot: dict[str, Any]) -> None:
    """capture_snapshot over a reader reproduces the reader's data (same format as fixtures)."""
    src = ReplayClusterClient(snapshot)
    snap = capture_snapshot(src, "copy", NS)
    again = ReplayClusterClient(snap)
    assert again.list_pods(NS) == src.list_pods(NS)
    assert again.describe_pod(NS, POD) == src.describe_pod(NS, POD)
    assert snap["source"] == "live-kind"

    class NoMetrics(ReplayClusterClient):
        def get_pod_metrics(self, namespace: str) -> list[dict[str, Any]]:
            raise ClusterError("metrics-server unavailable")

    assert capture_snapshot(NoMetrics(snapshot), "x", NS)["pod_metrics"] == []


def test_replay_writer_records() -> None:
    w = ReplayClusterWriter()
    assert isinstance(w, ClusterWriter)
    w.rollout_restart("n", "d")
    w.scale_deployment("n", "d", 2)
    w.rollback_deployment("n", "d")
    w.patch_resources("n", "d", "c", None, {"cpu": "1"})
    assert [a["op"] for a in w.applied] == [
        "rollout_restart",
        "scale_deployment",
        "rollback_deployment",
        "patch_resources",
    ]
