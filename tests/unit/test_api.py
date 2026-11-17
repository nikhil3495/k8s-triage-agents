from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import NS, POD, write_snapshot
from tests.fakes import ScriptedLlm, triage_script
from tests.unit.test_agent_flow import HYP
from triage.agents.factory import Models
from triage.api.app import RateLimiter, create_app
from triage.investigations import InvestigationService
from triage.k8s.replay import ReplayClusterWriter
from triage.runtime import Runtime

KEY = {"x-api-key": "test-key"}


@pytest.fixture
def client(
    runtime: Runtime, replay_reader: Any, snapshot: dict[str, Any], tmp_path: Path
) -> TestClient:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    snapshot["symptom"] = "pods not starting"
    write_snapshot(fixtures, snapshot)
    runtime.settings.replay_fixtures_dir = fixtures
    runtime.settings.default_scenario = "synthetic-image-pull"
    llm = ScriptedLlm(
        scripts=triage_script(
            NS,
            pod=POD,
            hypothesis=HYP,
            remediation={"action": "rollout_restart", "deployment": "checkout", "rationale": "r"},
        )
    )
    app = create_app(lambda: InvestigationService(runtime, Models(llm, llm, llm)))
    with TestClient(app) as c:
        yield c


def _wait(client: TestClient, inv: str) -> dict[str, Any]:
    for _ in range(200):
        r = client.get(f"/investigations/{inv}").json()
        if r["status"] in ("completed", "failed"):
            return r
        time.sleep(0.05)
    raise AssertionError("investigation did not finish")


def test_health_ready_scenarios_ui(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    ready = client.get("/readyz").json()
    assert ready["scenarios"] == ["synthetic-image-pull"]
    assert ready["runbook_chunks"] > 50
    assert client.get("/scenarios").json()[0]["namespace"] == NS
    assert "Kubernetes Incident Triage" in client.get("/").text
    assert client.get("/openapi.json").status_code == 200


def test_write_endpoints_require_api_key(client: TestClient) -> None:
    body = {"symptom": "pods not starting", "scenario": "synthetic-image-pull"}
    assert client.post("/investigations", json=body).status_code == 401
    assert (
        client.post("/investigations", json=body, headers={"x-api-key": "nope"}).status_code == 401
    )
    assert client.post("/investigations/inv_x/resume").status_code == 401


def test_end_to_end_with_approval_gate(client: TestClient, writer: ReplayClusterWriter) -> None:
    r = client.post(
        "/investigations",
        json={"symptom": "pods not starting", "scenario": "synthetic-image-pull"},
        headers=KEY,
    )
    assert r.status_code == 202
    inv = r.json()["id"]
    report = _wait(client, inv)
    assert report["status"] == "completed", report["error"]
    assert report["hypothesis"]["category"] == "image_pull"
    assert report["cost_usd"] > 0
    assert client.get("/investigations").json()[0]["id"] == inv

    [action] = client.get("/approvals").json()
    aid = action["id"]
    assert action["status"] == "pending"
    # No token / wrong token: blocked, nothing executed.
    assert client.post(f"/approvals/{aid}", json={"approve": True}).status_code == 403
    assert (
        client.post(
            f"/approvals/{aid}", json={"approve": True}, headers={"x-approver-token": "test-key"}
        ).status_code
        == 403
    )
    assert writer.applied == []
    # The API key is not an approver token, and the approver token is required.
    ok = client.post(
        f"/approvals/{aid}",
        json={"approve": True, "approver": "sre"},
        headers={"x-approver-token": "test-approver"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "executed"
    assert writer.applied[0]["op"] == "rollout_restart"
    again = client.post(
        f"/approvals/{aid}", json={"approve": True}, headers={"x-approver-token": "test-approver"}
    )
    assert again.status_code == 409
    assert (
        client.post(
            "/approvals/act_missing",
            json={"approve": True},
            headers={"x-approver-token": "test-approver"},
        ).status_code
        == 404
    )
    audit = [e["event"] for e in client.get(f"/approvals/{aid}/audit").json()]
    assert audit == ["proposed", "denied_bad_token", "denied_bad_token", "approved", "executed"]

    summary = client.get("/metrics/summary").json()
    assert summary["request_latency_ms"]["count"] >= 1
    assert summary["critic"]["verdicts"]["PASS"] >= 1
    assert summary["approvals"]["decisions"]["executed"] == 1
    assert "Supervisor" in summary["tokens_per_sec_by_agent"]
    assert summary["pricing"]["checked_on"]

    # SSE replays recorded steps then ends.
    with client.stream("GET", f"/investigations/{inv}/events") as s:
        body = "".join(s.iter_text())
    assert "event: step" in body
    assert "event: done" in body
    # Resuming a completed investigation is a no-op.
    assert client.post(f"/investigations/{inv}/resume", headers=KEY).json()["status"] == "completed"


def test_not_found_and_validation(client: TestClient) -> None:
    assert client.get("/investigations/inv_missing").status_code == 404
    assert client.get("/investigations/inv_missing/events").status_code == 404
    assert client.post("/investigations/inv_missing/resume", headers=KEY).status_code == 404
    r = client.post("/investigations", json={"symptom": "x" * 3, "scenario": "nope"}, headers=KEY)
    assert r.status_code == 404
    assert client.post("/investigations", json={"symptom": ""}, headers=KEY).status_code == 422


def test_rate_limiter() -> None:
    rl = RateLimiter(limit=2, window_s=60)
    rl.check()
    rl.check()
    with pytest.raises(Exception, match="rate limit"):
        rl.check()
