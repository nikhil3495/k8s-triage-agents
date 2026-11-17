"""Process-wide service container (DB engine, HITL queue, retriever, session service).

Built lazily from :class:`~triage.config.Settings`; tests replace pieces with
:func:`set_runtime`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from google.adk.sessions import BaseSessionService
from sqlalchemy import Engine

from triage.config import ClusterMode, Settings, get_settings
from triage.db import make_engine
from triage.hitl.service import ApprovalService
from triage.k8s.base import ClusterWriter
from triage.k8s.replay import ReplayClusterWriter
from triage.rag.retriever import HybridRetriever, load_or_build_retriever
from triage.sessions.service import make_session_service
from triage.telemetry.metrics import record_approval_decision


@dataclass
class Runtime:
    """Shared services for one process."""

    settings: Settings
    engine: Engine
    approvals: ApprovalService
    retriever: HybridRetriever
    session_service: BaseSessionService


_runtime: Runtime | None = None


def _writer_factory(settings: Settings) -> ClusterWriter:
    if settings.cluster_mode == ClusterMode.LIVE:
        from triage.k8s.live import LiveClusterWriter

        return LiveClusterWriter(context=settings.kube_context)
    return ReplayClusterWriter()


def build_runtime(settings: Settings | None = None) -> Runtime:
    """Construct all services from settings."""
    s = settings or get_settings()
    engine = make_engine(s.app_db_url)
    approvals = ApprovalService(
        engine,
        approver_token=s.approver_token.get_secret_value(),
        writer_factory=lambda: _writer_factory(s),
        ttl=timedelta(minutes=s.approval_ttl_minutes),
        on_decision=record_approval_decision,
    )
    return Runtime(
        settings=s,
        engine=engine,
        approvals=approvals,
        retriever=load_or_build_retriever(s),
        session_service=make_session_service(s.session_db_url),
    )


def get_runtime() -> Runtime:
    """Return (building on first use) the process runtime."""
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


def set_runtime(rt: Runtime | None) -> None:
    """Install a runtime (tests) or reset to lazy construction (``None``)."""
    global _runtime
    _runtime = rt
