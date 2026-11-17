"""The approval gate: nothing reaches the cluster writer without a valid human approval."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from triage.config import REPO_ROOT
from triage.db import make_engine
from triage.hitl.models import ActionStatus, ActionType, Risk, assess_risk, build_preview
from triage.hitl.service import ApprovalService, InvalidState, NotFound, Unauthorized
from triage.k8s.replay import ReplayClusterClient, ReplayClusterWriter

TOKEN = "s3cret-approver"


class SpyWriter(ReplayClusterWriter):
    pass


@pytest.fixture
def spy() -> SpyWriter:
    return SpyWriter()


@pytest.fixture
def svc(tmp_path: Path, spy: SpyWriter) -> ApprovalService:
    engine = make_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    return ApprovalService(engine, TOKEN, lambda: spy, ttl=timedelta(minutes=5))


def _propose(svc: ApprovalService, reader: Any = None, **kw: Any) -> Any:
    args: dict[str, Any] = {
        "action": "scale_deployment",
        "namespace": "eval-bad-image-tag",
        "deployment": "checkout",
        "params": {"replicas": 3},
        "requested_by": "agent:Supervisor",
        "rationale": "more capacity",
        "reader": reader,
    }
    args.update(kw)
    return svc.propose(**args)


def test_propose_never_executes(svc: ApprovalService, spy: SpyWriter) -> None:
    pa = _propose(svc)
    assert pa.status == ActionStatus.PENDING
    assert spy.applied == []
    assert [e["event"] for e in svc.audit_trail(pa.id)] == ["proposed"]


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x"])
def test_approval_requires_valid_token(svc: ApprovalService, spy: SpyWriter, token: Any) -> None:
    pa = _propose(svc)
    with pytest.raises(Unauthorized):
        svc.decide(pa.id, approve=True, token=token)
    assert spy.applied == []
    assert svc.get(pa.id).status == ActionStatus.PENDING
    assert svc.audit_trail(pa.id)[-1]["event"] == "denied_bad_token"


def test_approve_executes_once_and_is_audited(svc: ApprovalService, spy: SpyWriter) -> None:
    pa = _propose(svc)
    done = svc.decide(pa.id, approve=True, token=TOKEN, approver="alice")
    assert done.status == ActionStatus.EXECUTED
    assert done.decided_by == "alice"
    assert spy.applied == [
        {
            "op": "scale_deployment",
            "simulated": True,
            "namespace": "eval-bad-image-tag",
            "deployment": "checkout",
            "replicas": 3,
        }
    ]
    with pytest.raises(InvalidState):
        svc.decide(pa.id, approve=True, token=TOKEN)
    assert len(spy.applied) == 1
    events = [e["event"] for e in svc.audit_trail(pa.id)]
    assert events == ["proposed", "approved", "executed"]


def test_reject_never_executes(svc: ApprovalService, spy: SpyWriter) -> None:
    pa = _propose(svc)
    assert svc.decide(pa.id, approve=False, token=TOKEN).status == ActionStatus.REJECTED
    assert spy.applied == []


def test_expired_action_cannot_be_approved(tmp_path: Path, spy: SpyWriter) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    svc = ApprovalService(engine, TOKEN, lambda: spy, ttl=timedelta(seconds=-1))
    pa = _propose(svc)
    with pytest.raises(InvalidState):
        svc.decide(pa.id, approve=True, token=TOKEN)
    assert spy.applied == []
    assert svc.get(pa.id).status == ActionStatus.EXPIRED


def test_executor_failure_is_recorded(svc: ApprovalService) -> None:
    class Boom(ReplayClusterWriter):
        def scale_deployment(self, namespace: str, deployment: str, replicas: int) -> Any:
            raise RuntimeError("api down")

    svc._writer_factory = Boom
    pa = _propose(svc)
    out = svc.decide(pa.id, approve=True, token=TOKEN)
    assert out.status == ActionStatus.FAILED
    assert out.result == {"error": "api down"}


def test_unknown_action_and_bad_params(svc: ApprovalService) -> None:
    with pytest.raises(NotFound):
        svc.get("act_nope")
    with pytest.raises(ValueError, match="delete_namespace"):
        _propose(svc, action="delete_namespace")
    with pytest.raises(ValueError, match="replicas"):
        _propose(svc, params={"replicas": 500})
    with pytest.raises(ValueError, match="cpu and memory"):
        _propose(svc, action="patch_resources", params={"container": "web", "limits": {"gpu": "1"}})
    with pytest.raises(ValueError, match="non-empty"):
        ApprovalService(svc._engine, "", lambda: ReplayClusterWriter())


def test_all_action_types_dispatch(svc: ApprovalService, spy: SpyWriter) -> None:
    cases = [
        ("rollout_restart", {}),
        ("rollback_deployment", {}),
        ("patch_resources", {"container": "web", "limits": {"memory": "256Mi"}}),
    ]
    for action, params in cases:
        pa = _propose(svc, action=action, params=params)
        assert svc.decide(pa.id, approve=True, token=TOKEN).status == ActionStatus.EXECUTED
    assert [a["op"] for a in spy.applied] == [c[0] for c in cases]
    assert len(svc.list()) == 3
    assert len(svc.list(status=ActionStatus.EXECUTED)) == 3


def test_preview_and_risk(replay_reader: ReplayClusterClient) -> None:
    text, replicas = build_preview(
        replay_reader,
        ActionType.SCALE_DEPLOYMENT,
        replay_reader.namespace,
        "checkout",
        {"replicas": 0},
    )
    assert "spec.replicas: 2 -> 0" in text
    assert assess_risk(ActionType.SCALE_DEPLOYMENT, {"replicas": 0}, replicas) == Risk.HIGH
    assert assess_risk(ActionType.SCALE_DEPLOYMENT, {"replicas": 4}, replicas) == Risk.MEDIUM
    assert assess_risk(ActionType.ROLLOUT_RESTART, {}, replicas) == Risk.LOW
    text, _ = build_preview(
        replay_reader,
        ActionType.PATCH_RESOURCES,
        replay_reader.namespace,
        "checkout",
        {"container": "web", "limits": {"memory": "256Mi"}},
    )
    assert "resources.limits.memory: 128Mi -> 256Mi" in text
    text, _ = build_preview(
        replay_reader, ActionType.ROLLBACK_DEPLOYMENT, replay_reader.namespace, "checkout", {}
    )
    assert "nginx:1.27.99-doesnotexist" in text
    text, _ = build_preview(None, ActionType.ROLLOUT_RESTART, "ns", "x", {})
    assert "current state unavailable" in text


def test_no_agent_tool_can_reach_the_writer() -> None:
    """Static guarantee: agent-facing modules never import a ClusterWriter or the executor."""
    agent_modules = [
        *Path(REPO_ROOT / "src/triage/tools").glob("*.py"),
        *Path(REPO_ROOT / "src/triage/agents").glob("*.py"),
    ]
    forbidden = {"LiveClusterWriter", "ReplayClusterWriter", "ClusterWriter", "_execute", "decide"}
    for path in agent_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert not (names & forbidden), f"{path.name} references {names & forbidden}"
