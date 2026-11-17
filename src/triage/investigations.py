"""Investigation lifecycle: start, run, checkpoint, fail, resume, report.

One investigation == one ADK session. Every ADK event (with its state delta) is
persisted by the session service as it happens, and the app is configured with
``ResumabilityConfig(is_resumable=True)``, so after a crash
``runner.run_async(invocation_id=<last>)`` continues from the last persisted
event instead of starting over.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from google.adk.agents import RunConfig
from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner
from google.genai import types
from opentelemetry import trace
from sqlalchemy import insert, select, update

from triage import db, state_keys
from triage.agents.factory import Models, build_root_agent
from triage.config import ClusterMode
from triage.runtime import Runtime
from triage.telemetry import metrics
from triage.telemetry.plugin import (
    CURRENT_LEDGER,
    CURRENT_STEP_SINK,
    InvocationUsage,
    TelemetryPlugin,
)

log = logging.getLogger(__name__)
tracer = trace.get_tracer("triage.investigations")

APP_NAME = "k8s_triage"
USER_ID = "oncall"


class InvestigationNotFound(KeyError):
    """Unknown investigation id."""


class InvestigationService:
    """Owns the ADK runner and the investigations table."""

    def __init__(self, rt: Runtime, models: Models | None = None) -> None:
        """Build the agent tree and runner for this runtime."""
        self.rt = rt
        self.telemetry = TelemetryPlugin()
        if models is None and rt.settings.llm_backend == "baseline":
            from triage.agents.baseline import BaselineLlm

            llm = BaselineLlm()
            models = Models(supervisor=llm, worker=llm, critic=llm)
        app = App(
            name=APP_NAME,
            root_agent=build_root_agent(rt.settings, models),
            plugins=[self.telemetry],
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        self.runner = Runner(app=app, session_service=rt.session_service)
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}

    # ------------------------------------------------------------------ rows
    def _row(self, inv_id: str) -> dict[str, Any]:
        with self.rt.engine.connect() as conn:
            row = conn.execute(
                select(db.investigations).where(db.investigations.c.id == inv_id)
            ).first()
        if row is None:
            raise InvestigationNotFound(inv_id)
        return dict(row._mapping)

    def _update(self, inv_id: str, **values: Any) -> None:
        values["updated_at"] = datetime.now(UTC)
        with self.rt.engine.begin() as conn:
            conn.execute(
                update(db.investigations).where(db.investigations.c.id == inv_id).values(**values)
            )

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent investigations."""
        with self.rt.engine.connect() as conn:
            rows = conn.execute(
                select(db.investigations)
                .order_by(db.investigations.c.created_at.desc())
                .limit(limit)
            )
            return [dict(r._mapping) for r in rows]

    # ------------------------------------------------------------- lifecycle
    async def create(
        self,
        namespace: str,
        symptom: str,
        scenario: str | None = None,
        cluster_mode: ClusterMode | None = None,
    ) -> str:
        """Create the session + row. Returns the investigation id (status ``queued``)."""
        mode = cluster_mode or self.rt.settings.cluster_mode
        inv_id = f"inv_{uuid.uuid4().hex[:12]}"
        state: dict[str, Any] = {
            "investigation_id": inv_id,
            state_keys.NAMESPACE: namespace,
            state_keys.SYMPTOM: symptom,
            state_keys.SCENARIO: scenario,
            state_keys.CLUSTER_MODE: str(mode),
            state_keys.CRITIC_HISTORY: [],
            state_keys.CHECKPOINTS: [],
        }
        session = await self.rt.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, state=state
        )
        now = datetime.now(UTC)
        with self.rt.engine.begin() as conn:
            conn.execute(
                insert(db.investigations).values(
                    id=inv_id,
                    session_id=session.id,
                    user_id=USER_ID,
                    namespace=namespace,
                    symptom=symptom,
                    scenario=scenario,
                    cluster_mode=str(mode),
                    status="queued",
                    cost_usd=0.0,
                    input_tokens=0,
                    output_tokens=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        return inv_id

    def _sink(self, inv_id: str) -> Any:
        def emit(step: dict[str, Any]) -> None:
            step = {"at": datetime.now(UTC).isoformat(), **step}
            with self.rt.engine.begin() as conn:
                conn.execute(
                    insert(db.investigation_steps).values(
                        investigation_id=inv_id, at=datetime.now(UTC), step=step
                    )
                )
            for q in self._subscribers.get(inv_id, []):
                q.put_nowait({"event": "step", "data": step})

        return emit

    def _publish(self, inv_id: str, event: str, data: dict[str, Any]) -> None:
        for q in self._subscribers.get(inv_id, []):
            q.put_nowait({"event": event, "data": data})

    async def run(self, inv_id: str, *, resume: bool = False) -> dict[str, Any]:
        """Run (or resume) an investigation to completion. Returns the report."""
        row = self._row(inv_id)
        if resume and row["status"] == "completed":
            return await self.report(inv_id)
        if not resume and row["status"] != "queued":
            raise ValueError(f"investigation {inv_id} is {row['status']}; use resume")
        invocation_id = row["last_invocation_id"] if resume else None
        message = None
        if invocation_id is None:
            message = types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=(
                            f"Investigate namespace {row['namespace']!r}. Symptom: {row['symptom']}"
                        )
                    )
                ],
            )
        ledger = InvocationUsage()
        ledger_token = CURRENT_LEDGER.set(ledger)
        sink_token = CURRENT_STEP_SINK.set(self._sink(inv_id))
        self._update(inv_id, status="running", error=None)
        self._publish(inv_id, "status", {"status": "running", "resume": resume})
        start = time.perf_counter()
        status = "completed"
        error: str | None = None
        with tracer.start_as_current_span(
            "triage.investigation",
            attributes={
                "investigation.id": inv_id,
                "k8s.namespace": row["namespace"],
                "triage.cluster_mode": row["cluster_mode"],
                "triage.resume": resume,
            },
        ) as span:
            trace_id = format(span.get_span_context().trace_id, "032x")
            try:
                async for event in self.runner.run_async(
                    user_id=row["user_id"],
                    session_id=row["session_id"],
                    invocation_id=invocation_id,
                    new_message=message,
                    run_config=RunConfig(max_llm_calls=self.rt.settings.max_llm_calls),
                ):
                    if event.invocation_id and event.invocation_id != invocation_id:
                        invocation_id = event.invocation_id
                        self._update(inv_id, last_invocation_id=invocation_id)
                    self._publish(
                        inv_id,
                        "event",
                        {"author": event.author, "final": event.is_final_response()},
                    )
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, error))
                log.exception("investigation failed", extra={"investigation_id": inv_id})
            finally:
                CURRENT_LEDGER.reset(ledger_token)
                CURRENT_STEP_SINK.reset(sink_token)
            latency_ms = (time.perf_counter() - start) * 1000
            totals = ledger.totals
            span.set_attributes(
                {
                    "latency_ms": round(latency_ms, 1),
                    "llm.cost_usd": totals["cost_usd"],
                    "llm.input_tokens": totals["input_tokens"],
                    "llm.output_tokens": totals["output_tokens"],
                    "triage.status": status,
                }
            )
        prev = self._row(inv_id)
        self._update(
            inv_id,
            status=status,
            error=error,
            cost_usd=float(prev["cost_usd"] or 0) + totals["cost_usd"],
            input_tokens=int(prev["input_tokens"] or 0) + totals["input_tokens"],
            output_tokens=int(prev["output_tokens"] or 0) + totals["output_tokens"],
            latency_ms=float(prev["latency_ms"] or 0) + latency_ms,
            trace_id=trace_id,
        )
        await self._store_usage(row, ledger)
        metrics.record_request(latency_ms, totals["cost_usd"], status)
        report = await self.report(inv_id)
        self._publish(inv_id, "done", {"status": status, "error": error})
        return report

    async def _store_usage(self, row: dict[str, Any], ledger: InvocationUsage) -> None:
        """Merge this run's per-agent usage into the session (survives restarts)."""
        from google.adk.events import Event, EventActions

        session = await self.rt.session_service.get_session(
            app_name=APP_NAME, user_id=row["user_id"], session_id=row["session_id"]
        )
        if session is None:
            return
        merged = InvocationUsage()
        prev = session.state.get("usage") or {}
        for name, u in (prev.get("by_agent") or {}).items():
            a = merged.agent(name)
            a.llm_calls, a.tool_calls = u["llm_calls"], u["tool_calls"]
            a.input_tokens, a.output_tokens = u["input_tokens"], u["output_tokens"]
            a.cost_usd, a.llm_latency_ms = u["cost_usd"], u["llm_latency_ms"]
        merged.merge(ledger)
        await self.rt.session_service.append_event(
            session,
            Event(
                author="system",
                invocation_id=row["last_invocation_id"] or "usage",
                actions=EventActions(state_delta={"usage": merged.to_dict()}),
            ),
        )

    def start_background(self, inv_id: str, *, resume: bool = False) -> None:
        """Schedule :meth:`run` on the event loop (API path)."""
        task = asyncio.create_task(self.run(inv_id, resume=resume))
        self._tasks[inv_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(inv_id, None))

    async def subscribe(self, inv_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield live events for an investigation until it finishes."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[inv_id].append(q)
        try:
            for step in self.steps(inv_id):
                yield {"event": "step", "data": step}
            if self._row(inv_id)["status"] in ("completed", "failed"):
                yield {"event": "done", "data": {"status": self._row(inv_id)["status"]}}
                return
            while True:
                msg = await q.get()
                yield msg
                if msg["event"] == "done":
                    return
        finally:
            self._subscribers[inv_id].remove(q)

    # ---------------------------------------------------------------- report
    def steps(self, inv_id: str) -> list[dict[str, Any]]:
        """All recorded agent steps, in order."""
        with self.rt.engine.connect() as conn:
            rows = conn.execute(
                select(db.investigation_steps.c.step)
                .where(db.investigation_steps.c.investigation_id == inv_id)
                .order_by(db.investigation_steps.c.id)
            )
            return [r.step for r in rows]

    async def report(self, inv_id: str) -> dict[str, Any]:
        """Status, steps, hypothesis, critic verdicts, approvals and cost."""
        row = self._row(inv_id)
        session = await self.rt.session_service.get_session(
            app_name=APP_NAME, user_id=row["user_id"], session_id=row["session_id"]
        )
        state: dict[str, Any] = dict(session.state) if session else {}
        verdict = state.get(state_keys.CRITIC_VERDICT)
        return {
            "id": inv_id,
            "status": row["status"],
            "error": row["error"],
            "namespace": row["namespace"],
            "symptom": row["symptom"],
            "scenario": row["scenario"],
            "cluster_mode": row["cluster_mode"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "hypothesis": state.get(state_keys.HYPOTHESIS),
            "critic": {
                "final_verdict": verdict.get("verdict") if isinstance(verdict, dict) else None,
                "rounds": state.get(state_keys.CRITIC_HISTORY) or [],
            },
            "checkpoints": state.get(state_keys.CHECKPOINTS) or [],
            "pending_actions": [
                a.model_dump(mode="json") for a in self.rt.approvals.list(investigation_id=inv_id)
            ],
            "runbook_citations": state.get("runbook_citations") or [],
            "usage": state.get("usage") or {},
            "cost_usd": round(float(row["cost_usd"] or 0), 6),
            "latency_ms": row["latency_ms"],
            "trace_id": row["trace_id"],
            "steps": self.steps(inv_id),
        }
