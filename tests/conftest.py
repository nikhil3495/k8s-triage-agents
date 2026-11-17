from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from triage.config import REPO_ROOT, ClusterMode, Settings, TraceExporter
from triage.db import make_engine
from triage.hitl.service import ApprovalService
from triage.k8s.replay import ReplayClusterClient, ReplayClusterWriter
from triage.rag.chunking import load_corpus
from triage.rag.retriever import HybridRetriever
from triage.runtime import Runtime, set_runtime
from triage.sessions.service import make_session_service
from triage.telemetry.metrics import record_approval_decision, reset_summary
from triage.tools.registry import registry

NS = "eval-bad-image-tag"
POD = "checkout-7d4b9c-abcde"


def synthetic_snapshot() -> dict[str, Any]:
    """Hand-written, clearly synthetic snapshot for unit tests (not a recorded fixture)."""
    return {
        "scenario": "synthetic-image-pull",
        "namespace": NS,
        "captured_at": "2026-11-17T00:00:00+00:00",
        "source": "synthetic-unit-test",
        "pods": [
            {
                "name": POD,
                "phase": "Pending",
                "ready": "0/1",
                "restarts": 0,
                "reason": "ImagePullBackOff",
                "node": "kind-control-plane",
                "owner": "checkout-7d4b9c",
                "labels": {"app": "checkout"},
            }
        ],
        "pod_details": {
            POD: {
                "name": POD,
                "namespace": NS,
                "labels": {"app": "checkout"},
                "node": "kind-control-plane",
                "phase": "Pending",
                "conditions": [{"type": "Ready", "status": "False"}],
                "containers": [
                    {
                        "name": "web",
                        "image": "nginx:1.27.99-doesnotexist",
                        "resources": {"limits": {"cpu": "200m", "memory": "128Mi"}},
                        "restart_count": 0,
                        "ready": False,
                        "state": {
                            "state": "waiting",
                            "reason": "ImagePullBackOff",
                            "message": 'Back-off pulling image "nginx:1.27.99-doesnotexist"',
                        },
                        "last_state": {"state": "unknown"},
                    }
                ],
                "volumes": [],
            }
        },
        "logs": {POD: {"current": "", "previous": ""}},
        "events": [
            {
                "type": "Warning",
                "reason": "Failed",
                "object": f"Pod/{POD}",
                "message": 'Failed to pull image "nginx:1.27.99-doesnotexist": not found',
                "count": 4,
            },
            {
                "type": "Normal",
                "reason": "Scheduled",
                "object": f"Pod/{POD}",
                "message": "Successfully assigned",
                "count": 1,
            },
        ],
        "deployments": [
            {
                "name": "checkout",
                "revision": "2",
                "replicas": 2,
                "ready_replicas": 0,
                "unavailable_replicas": 2,
                "selector": {"app": "checkout"},
                "containers": [
                    {
                        "name": "web",
                        "image": "nginx:1.27.99-doesnotexist",
                        "resources": {"limits": {"cpu": "200m", "memory": "128Mi"}},
                    }
                ],
                "conditions": [],
            }
        ],
        "services": [
            {
                "name": "checkout",
                "type": "ClusterIP",
                "selector": {"app": "checkout"},
                "ports": [],
                "ready_endpoints": 0,
                "not_ready_endpoints": 0,
            }
        ],
        "nodes": [
            {
                "name": "kind-control-plane",
                "ready": "True",
                "pressure": [],
                "allocatable": {"cpu": "8", "memory": "16Gi"},
                "taints": [],
            }
        ],
        "pod_metrics": [
            {"pod": POD, "containers": [{"name": "web", "cpu_m": 190.0, "memory_mib": 20.0}]}
        ],
        "node_metrics": [{"node": "kind-control-plane", "cpu_m": 800.0, "memory_mib": 2048.0}],
    }


@pytest.fixture
def snapshot() -> dict[str, Any]:
    return synthetic_snapshot()


@pytest.fixture
def replay_reader(snapshot: dict[str, Any]) -> Iterator[ReplayClusterClient]:
    reader = ReplayClusterClient(snapshot)
    registry.set_factory(lambda _mode, _scenario: reader)
    yield reader
    registry.set_factory(lambda m, s: (_ for _ in ()).throw(AssertionError("reader not set")))


@pytest.fixture(scope="session")
def retriever() -> HybridRetriever:
    return HybridRetriever(load_corpus(REPO_ROOT / "runbooks"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        cluster_mode=ClusterMode.REPLAY,
        session_db_url=f"sqlite+aiosqlite:///{(tmp_path / 'sessions.db').as_posix()}",
        app_db_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        triage_api_key="test-key",  # type: ignore[arg-type]
        approver_token="test-approver",  # type: ignore[arg-type]
        trace_exporter=TraceExporter.NONE,
        max_critic_rounds=2,
    )


@pytest.fixture
def writer() -> ReplayClusterWriter:
    return ReplayClusterWriter()


@pytest.fixture
def runtime(
    settings: Settings, retriever: HybridRetriever, writer: ReplayClusterWriter
) -> Iterator[Runtime]:
    engine = make_engine(settings.app_db_url)
    rt = Runtime(
        settings=settings,
        engine=engine,
        approvals=ApprovalService(
            engine,
            "test-approver",
            lambda: writer,
            ttl=timedelta(minutes=30),
            on_decision=record_approval_decision,
        ),
        retriever=retriever,
        session_service=make_session_service(settings.session_db_url),
    )
    set_runtime(rt)
    reset_summary()
    yield rt
    set_runtime(None)


def write_snapshot(tmp_path: Path, snap: dict[str, Any]) -> Path:
    path = tmp_path / f"{snap['scenario']}.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return path
