"""Application tables (investigations index, HITL queue, audit log).

ADK owns the session/event tables (see ``triage.sessions``); these tables hold
the application-level records the API serves.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Engine,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)

metadata = MetaData()

investigations = Table(
    "investigations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("session_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("namespace", String(253), nullable=False),
    Column("symptom", Text, nullable=False),
    Column("scenario", String(128)),
    Column("cluster_mode", String(16), nullable=False),
    Column("status", String(32), nullable=False),
    Column("last_invocation_id", String(128)),
    Column("error", Text),
    Column("cost_usd", Float, default=0.0),
    Column("input_tokens", Integer, default=0),
    Column("output_tokens", Integer, default=0),
    Column("latency_ms", Float),
    Column("trace_id", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

pending_actions = Table(
    "pending_actions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("investigation_id", String(64)),
    Column("action", String(64), nullable=False),
    Column("namespace", String(253), nullable=False),
    Column("deployment", String(253), nullable=False),
    Column("params", JSON, nullable=False),
    Column("preview", Text, nullable=False),
    Column("risk", String(16), nullable=False),
    Column("rationale", Text),
    Column("requested_by", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True)),
    Column("decided_by", String(128)),
    Column("result", JSON),
)

investigation_steps = Table(
    "investigation_steps",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("investigation_id", String(64), nullable=False, index=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("step", JSON, nullable=False),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("event", String(64), nullable=False),
    Column("action_id", String(64)),
    Column("detail", JSON),
)


def make_engine(url: str) -> Engine:
    """Create an engine, ensuring the parent directory of a SQLite file exists."""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    metadata.create_all(engine)
    return engine
