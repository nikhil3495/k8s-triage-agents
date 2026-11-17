install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt:
	uv run ruff format .
	uv run ruff check . --fix

test:
	uv run pytest --cov

index:
	uv run python -m scripts.build_index

run:
	uv run uvicorn triage.api.app:app --reload --port 8080

adk-web:
	uv run adk web adk_agents

eval-offline:
	uv run python -m eval.run_eval --mode replay --model scripted

eval-live:
	uv run python -m eval.run_eval --mode live --model gemini

record:
	uv run python -m scripts.record_scenario

secrets-check:
	uv run python scripts/secret_scan.py

.PHONY: install lint fmt test index run adk-web eval-offline eval-live record secrets-check
