"""FastAPI service: investigations, approvals, SSE stream, metrics and the web UI."""

from __future__ import annotations

import hmac
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from triage import __version__
from triage.config import ClusterMode, get_settings
from triage.hitl.models import ActionStatus
from triage.hitl.service import InvalidState, NotFound, Unauthorized
from triage.investigations import InvestigationNotFound, InvestigationService
from triage.k8s.replay import list_scenarios, load_snapshot
from triage.logging_setup import configure_logging
from triage.runtime import get_runtime
from triage.telemetry import metrics
from triage.telemetry.pricing import PRICES, PRICES_CHECKED_ON
from triage.telemetry.setup import setup_telemetry

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class StartInvestigation(BaseModel):
    """Body of ``POST /investigations``."""

    namespace: str | None = Field(default=None, max_length=253)
    symptom: str = Field(min_length=3, max_length=500)
    scenario: str | None = Field(
        default=None, description="Replay scenario (replay mode); its namespace is used"
    )


class Decision(BaseModel):
    """Body of ``POST /approvals/{id}``."""

    approve: bool
    approver: str = Field(default="approver", max_length=128)


class RateLimiter:
    """Sliding-window limiter for LLM-spending endpoints (per instance)."""

    def __init__(self, limit: int, window_s: float) -> None:
        """Allow ``limit`` calls per ``window_s`` seconds."""
        self.limit, self.window = limit, window_s
        self.hits: deque[float] = deque()

    def check(self) -> None:
        """Raise 429 when the window is full."""
        now = time.monotonic()
        while self.hits and now - self.hits[0] > self.window:
            self.hits.popleft()
        if len(self.hits) >= self.limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "investigation rate limit hit")
        self.hits.append(now)


def service(request: Request) -> InvestigationService:
    """Dependency: the investigation service created at startup."""
    svc: InvestigationService = request.app.state.service
    return svc


Svc = Annotated[InvestigationService, Depends(service)]


def require_api_key(svc: Svc, x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Dependency: constant-time check of the ``X-API-Key`` header."""
    expected = svc.rt.settings.triage_api_key.get_secret_value().encode()
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key")


def create_app(service_factory: Callable[[], InvestigationService] | None = None) -> FastAPI:
    """Build the app. ``service_factory`` lets tests inject a scripted-LLM service."""
    settings = get_settings()
    limiter = RateLimiter(limit=20, window_s=3600)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        setup_telemetry(settings)
        app.state.service = (service_factory or (lambda: InvestigationService(get_runtime())))()
        log.info("triage api started", extra={"cluster_mode": str(settings.cluster_mode)})
        yield

    app = FastAPI(
        title="k8s-triage-agents",
        version=__version__,
        description="Multi-agent Kubernetes incident triage (Google ADK + Gemini).",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------- health
    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    async def readyz(svc: Svc) -> dict[str, Any]:
        rt = svc.rt
        return {
            "status": "ready",
            "cluster_mode": str(rt.settings.cluster_mode),
            "retrieval_mode": rt.retriever.mode,
            "runbook_chunks": len(rt.retriever.chunks),
            "scenarios": list_scenarios(rt.settings.replay_fixtures_dir),
            "models": {
                "supervisor": rt.settings.supervisor_model,
                "worker": rt.settings.worker_model,
                "critic": rt.settings.critic_model,
            },
            "llm_configured": rt.settings.gemini_api_key is not None,
        }

    # ---------------------------------------------------------- scenarios
    @app.get("/scenarios", tags=["investigations"])
    async def scenarios(svc: Svc) -> list[dict[str, Any]]:
        out = []
        fx = svc.rt.settings.replay_fixtures_dir
        for name in list_scenarios(fx):
            snap = load_snapshot(fx / f"{name}.json")
            out.append(
                {
                    "name": name,
                    "namespace": snap["namespace"],
                    "symptom": snap.get("symptom", ""),
                    "captured_at": snap.get("captured_at"),
                    "source": snap.get("source"),
                }
            )
        return out

    # ------------------------------------------------------ investigations
    @app.post(
        "/investigations",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_api_key)],
        tags=["investigations"],
    )
    async def start(body: StartInvestigation, svc: Svc) -> dict[str, Any]:
        limiter.check()
        rt = svc.rt
        namespace = body.namespace
        if rt.settings.cluster_mode == ClusterMode.REPLAY:
            scenario = body.scenario or rt.settings.default_scenario
            if scenario not in list_scenarios(rt.settings.replay_fixtures_dir):
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown scenario {scenario!r}")
            namespace = load_snapshot(rt.settings.replay_fixtures_dir / f"{scenario}.json")[
                "namespace"
            ]
        else:
            scenario = None
            if not namespace:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "namespace required")
        inv_id = await svc.create(str(namespace), body.symptom, scenario=scenario)
        svc.start_background(inv_id)
        return {"id": inv_id, "status": "queued", "events": f"/investigations/{inv_id}/events"}

    @app.get("/investigations", tags=["investigations"])
    async def list_investigations(svc: Svc, limit: int = 20) -> list[dict[str, Any]]:
        return svc.list_recent(min(limit, 100))

    @app.get("/investigations/{inv_id}", tags=["investigations"])
    async def get_investigation(inv_id: str, svc: Svc) -> dict[str, Any]:
        try:
            return await svc.report(inv_id)
        except InvestigationNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "investigation not found") from exc

    @app.post(
        "/investigations/{inv_id}/resume",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_api_key)],
        tags=["investigations"],
    )
    async def resume(inv_id: str, svc: Svc) -> dict[str, Any]:
        try:
            row = await svc.report(inv_id)
        except InvestigationNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "investigation not found") from exc
        if row["status"] == "running" and inv_id in svc._tasks:
            raise HTTPException(status.HTTP_409_CONFLICT, "investigation is still running")
        if row["status"] == "completed":
            return {"id": inv_id, "status": "completed"}
        svc.start_background(inv_id, resume=True)
        return {"id": inv_id, "status": "resuming", "events": f"/investigations/{inv_id}/events"}

    @app.get("/investigations/{inv_id}/events", tags=["investigations"])
    async def events(inv_id: str, svc: Svc) -> EventSourceResponse:
        try:
            await svc.report(inv_id)
        except InvestigationNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "investigation not found") from exc

        async def gen() -> AsyncIterator[dict[str, Any]]:
            import json

            async for msg in svc.subscribe(inv_id):
                yield {"event": msg["event"], "data": json.dumps(msg["data"], default=str)}

        return EventSourceResponse(gen())

    # ----------------------------------------------------------- approvals
    @app.get("/approvals", tags=["approvals"])
    async def approvals(
        svc: Svc, status_filter: ActionStatus | None = None
    ) -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in svc.rt.approvals.list(status=status_filter)]

    @app.post("/approvals/{action_id}", tags=["approvals"])
    async def decide(
        action_id: str,
        body: Decision,
        svc: Svc,
        x_approver_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        try:
            pa = svc.rt.approvals.decide(
                action_id, approve=body.approve, token=x_approver_token, approver=body.approver
            )
        except Unauthorized as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid approver token") from exc
        except NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such action") from exc
        except InvalidState as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return pa.model_dump(mode="json")

    @app.get("/approvals/{action_id}/audit", tags=["approvals"])
    async def audit(action_id: str, svc: Svc) -> list[dict[str, Any]]:
        return [{**e, "at": e["at"].isoformat()} for e in svc.rt.approvals.audit_trail(action_id)]

    # ------------------------------------------------------------- metrics
    @app.get("/metrics/summary", tags=["ops"])
    async def metrics_summary() -> dict[str, Any]:
        return {
            **metrics.summary(),
            "pricing": {
                "checked_on": PRICES_CHECKED_ON,
                "usd_per_1m_tokens": {m: vars(p) for m, p in PRICES.items()},
            },
        }

    # ------------------------------------------------------------------ UI
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app


app = create_app()
