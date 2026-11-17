# syntax=docker/dockerfile:1.7
# ---------- build stage: resolve deps with uv, build the RAG index ----------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY runbooks ./runbooks
COPY scripts ./scripts
RUN uv sync --locked --no-dev
# Embeddings are added only when a GEMINI_API_KEY build secret is supplied;
# otherwise the index is lexical-only and /readyz reports retrieval_mode=bm25.
RUN --mount=type=secret,id=gemini_api_key,required=false \
    GEMINI_API_KEY="$(cat /run/secrets/gemini_api_key 2>/dev/null || true)" \
    /app/.venv/bin/python -m scripts.build_index

# ---------- runtime stage: slim, non-root ----------
FROM python:3.12-slim AS runtime
RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --from=build --chown=app:app /app/src /app/src
COPY --from=build --chown=app:app /app/runbooks /app/runbooks
COPY --from=build --chown=app:app /app/data/rag_index.json /app/data/rag_index.json
COPY --chown=app:app eval/fixtures /app/eval/fixtures
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PORT=8080 \
    CLUSTER_MODE=replay SESSION_DB_URL=sqlite+aiosqlite:////tmp/sessions.db \
    APP_DB_URL=sqlite:////tmp/triage.db RAG_INDEX_PATH=/app/data/rag_index.json \
    RUNBOOKS_DIR=/app/runbooks REPLAY_FIXTURES_DIR=/app/eval/fixtures
USER app
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["sh", "-c", "exec uvicorn triage.api.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
