"""Persistent ADK session service selection.

``SESSION_DB_URL`` forms:
- ``sqlite+aiosqlite:///./data/sessions.db`` (local default)
- ``postgresql+asyncpg://user:pass@host/db`` (Cloud SQL / any Postgres)
- ``vertexai://<project>/<location>/<agent_engine_id>`` (Vertex AI Agent Engine sessions)
- ``memory://`` (tests only - not durable)
"""

from __future__ import annotations

from pathlib import Path

from google.adk.sessions import (
    BaseSessionService,
    DatabaseSessionService,
    InMemorySessionService,
    VertexAiSessionService,
)


def make_session_service(url: str) -> BaseSessionService:
    """Return the ADK session service for ``url`` (see module docstring)."""
    if url.startswith("memory://"):
        return InMemorySessionService()
    if url.startswith("vertexai://"):
        project, location, engine_id = url.removeprefix("vertexai://").split("/", 2)
        return VertexAiSessionService(project=project, location=location, agent_engine_id=engine_id)
    if url.startswith("sqlite") and ":///" in url:
        path = url.split(":///", 1)[1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return DatabaseSessionService(db_url=url)
