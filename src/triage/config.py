"""Application settings, loaded from environment variables (and `.env` locally)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class ClusterMode(StrEnum):
    """Where investigation tools read cluster state from."""

    LIVE = "live"
    REPLAY = "replay"


class TraceExporter(StrEnum):
    """Which OpenTelemetry span exporter to install."""

    NONE = "none"
    CONSOLE = "console"
    OTLP = "otlp"
    GCP = "gcp"


class Settings(BaseSettings):
    """Runtime configuration. Every field maps to an upper-case env var."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Models (see docs/DECISIONS.md#models) ---
    supervisor_model: str = "gemini-3.8-flash"
    worker_model: str = "gemini-3.1-flash-lite"
    critic_model: str = "gemini-3.8-flash"
    embedding_model: str = "gemini-embedding-001"
    gemini_api_key: SecretStr | None = None

    # --- Agent loop guardrails ---
    max_critic_rounds: int = Field(default=2, ge=1, le=5)
    max_tool_calls_per_agent: int = Field(default=8, ge=1, le=50)
    max_llm_calls: int = Field(default=60, ge=5, le=500)

    # --- Cluster access ---
    cluster_mode: ClusterMode = ClusterMode.REPLAY
    replay_fixtures_dir: Path = REPO_ROOT / "eval" / "fixtures"
    default_scenario: str = "bad-image-tag"
    kube_context: str | None = None

    # --- Persistence ---
    session_db_url: str = "sqlite+aiosqlite:///./data/sessions.db"
    app_db_url: str = "sqlite:///./data/triage.db"

    # --- RAG ---
    runbooks_dir: Path = REPO_ROOT / "runbooks"
    rag_index_path: Path = REPO_ROOT / "data" / "rag_index.json"

    # --- Security ---
    triage_api_key: SecretStr = SecretStr("dev-api-key")
    approver_token: SecretStr = SecretStr("dev-approver-token")
    approval_ttl_minutes: int = 60

    # --- Telemetry ---
    trace_exporter: TraceExporter = TraceExporter.NONE
    otlp_endpoint: str = "http://localhost:4318"
    gcp_project: str | None = None
    enable_cloud_monitoring: bool = False
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
