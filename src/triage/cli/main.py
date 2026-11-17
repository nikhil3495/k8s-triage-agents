"""``triage`` command-line interface.

Runs in-process by default (uses ``.env`` settings), or against a deployed API
with ``--api https://...``.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Any

import httpx
import typer

from triage.config import ClusterMode, get_settings

app = typer.Typer(help="Multi-agent Kubernetes incident triage.", no_args_is_help=True)

ApiOpt = Annotated[str | None, typer.Option(help="Remote API base URL (default: run in-process)")]


def _print(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _summary(report: dict[str, Any]) -> None:
    h = report.get("hypothesis") or {}
    typer.secho(f"\n[{report['status']}] investigation {report['id']}", bold=True)
    if report.get("error"):
        typer.secho(f"error: {report['error']}", fg="red")
    if h:
        typer.echo(f"root cause : {h.get('root_cause')}")
        typer.echo(f"category   : {h.get('category')}  confidence {h.get('confidence')}")
        for e in h.get("evidence", []):
            typer.echo(f"  - [{e['source']}] {e['snippet']}")
        typer.echo(f"runbooks   : {', '.join(h.get('runbook_refs', []))}")
    rounds = report.get("critic", {}).get("rounds", [])
    typer.echo("critic     : " + " -> ".join(r["verdict"] for r in rounds))
    for a in report.get("pending_actions", []):
        typer.echo(f"pending    : {a['id']} {a['action']} {a['deployment']} ({a['risk']} risk)")
    typer.echo(f"cost       : ${report.get('cost_usd', 0):.5f}  trace {report.get('trace_id')}")


async def _local_investigate(namespace: str, symptom: str, scenario: str | None) -> dict[str, Any]:
    from triage.investigations import InvestigationService
    from triage.runtime import get_runtime
    from triage.telemetry.setup import setup_telemetry

    setup_telemetry(get_settings())
    svc = InvestigationService(get_runtime())
    inv = await svc.create(namespace, symptom, scenario=scenario)
    typer.echo(f"investigation {inv} started")
    return await svc.run(inv)


@app.command()
def investigate(
    symptom: Annotated[str, typer.Option(help="What is wrong, in plain words")],
    namespace: Annotated[str | None, typer.Option(help="Namespace (live mode)")] = None,
    scenario: Annotated[str | None, typer.Option(help="Replay scenario (replay mode)")] = None,
    api: ApiOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run an investigation and print the hypothesis."""
    s = get_settings()
    if api:
        headers = {"x-api-key": os.environ.get("TRIAGE_API_KEY", "")}
        body = {"namespace": namespace, "symptom": symptom, "scenario": scenario}
        with httpx.Client(base_url=api, timeout=30) as c:
            inv = c.post("/investigations", json=body, headers=headers).raise_for_status().json()
            typer.echo(f"investigation {inv['id']} started; streaming…")
            with c.stream("GET", f"/investigations/{inv['id']}/events", timeout=None) as r:
                for line in r.iter_lines():
                    if line.startswith("data:") and '"kind"' in line:
                        step = json.loads(line[5:])
                        what = step.get("tool") or ",".join(step.get("calls") or []) or "answer"
                        typer.echo(f"  {step['agent']:>13} | {step['kind']:4} | {what}")
            report = c.get(f"/investigations/{inv['id']}").raise_for_status().json()
    else:
        if s.cluster_mode == ClusterMode.REPLAY:
            from triage.k8s.replay import ReplayClusterClient

            scenario = scenario or s.default_scenario
            namespace = ReplayClusterClient.from_scenario(s.replay_fixtures_dir, scenario).namespace
        elif not namespace:
            raise typer.BadParameter("--namespace is required in live mode")
        report = asyncio.run(_local_investigate(str(namespace), symptom, scenario))
    _print(report) if as_json else _summary(report)


@app.command()
def resume(investigation_id: str, api: ApiOpt = None) -> None:
    """Resume a failed/interrupted investigation from its last checkpoint."""
    if api:
        headers = {"x-api-key": os.environ.get("TRIAGE_API_KEY", "")}
        r = httpx.post(f"{api}/investigations/{investigation_id}/resume", headers=headers)
        _print(r.raise_for_status().json())
        return
    from triage.investigations import InvestigationService
    from triage.runtime import get_runtime

    _summary(asyncio.run(InvestigationService(get_runtime()).run(investigation_id, resume=True)))


@app.command()
def approve(
    action_id: str,
    reject: Annotated[bool, typer.Option("--reject", help="Reject instead of approve")] = False,
    token: Annotated[str | None, typer.Option(envvar="APPROVER_TOKEN")] = None,
    approver: Annotated[str, typer.Option()] = os.environ.get("USER", "cli"),
    api: ApiOpt = None,
) -> None:
    """Approve (and execute) or reject a pending remediation."""
    if api:
        r = httpx.post(
            f"{api}/approvals/{action_id}",
            json={"approve": not reject, "approver": approver},
            headers={"x-approver-token": token or ""},
        )
        _print(r.raise_for_status().json())
        return
    from triage.runtime import get_runtime

    pa = get_runtime().approvals.decide(
        action_id, approve=not reject, token=token, approver=approver
    )
    _print(pa.model_dump(mode="json"))


@app.command()
def approvals(api: ApiOpt = None) -> None:
    """List remediation requests."""
    if api:
        _print(httpx.get(f"{api}/approvals").raise_for_status().json())
        return
    from triage.runtime import get_runtime

    _print([a.model_dump(mode="json") for a in get_runtime().approvals.list()])


@app.command()
def scenarios() -> None:
    """List recorded replay scenarios."""
    from triage.k8s.replay import list_scenarios

    for name in list_scenarios(get_settings().replay_fixtures_dir):
        typer.echo(name)


if __name__ == "__main__":
    app()
