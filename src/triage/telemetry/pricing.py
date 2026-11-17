"""Per-model token prices used to compute cost per LLM call and per request.

Prices are USD per 1M tokens, paid tier "Standard", text modality, from
https://ai.google.dev/gemini-api/docs/pricing (redirects to aistudio.google.com/docs/pricing).
PRICES_CHECKED_ON records when they were last verified. Override or extend at
runtime with the ``MODEL_PRICES_JSON`` env var, e.g.
``{"my-model": {"input": 0.5, "output": 2.0}}``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

PRICES_CHECKED_ON = "2026-11-17"


@dataclass(frozen=True)
class Price:
    """USD per 1M input / output tokens (output includes thinking tokens)."""

    input: float
    output: float


# Note: gemini-3.8-flash / 3.6-flash list $0.75 / $3.75 through 2026-12-31 and
# $1.50 / $7.50 from 2027-01-01. The table uses the current price.
DEFAULT_PRICES: dict[str, Price] = {
    "gemini-3.8-flash": Price(0.75, 3.75),
    "gemini-3.6-flash": Price(0.75, 3.75),
    "gemini-3.5-flash-lite": Price(0.30, 2.50),
    "gemini-3.1-flash-lite": Price(0.25, 1.50),
    "gemini-3.1-pro-preview": Price(2.00, 12.00),  # <=200k-token prompts
    "gemini-2.5-flash": Price(0.30, 2.50),
    "gemini-2.5-flash-lite": Price(0.10, 0.40),
    "gemini-2.5-pro": Price(1.25, 10.00),  # <=200k-token prompts
    "gemini-embedding-001": Price(0.15, 0.0),
}


def _load_prices() -> dict[str, Price]:
    prices = dict(DEFAULT_PRICES)
    override = os.environ.get("MODEL_PRICES_JSON")
    if override:
        for model, p in json.loads(override).items():
            prices[model] = Price(float(p["input"]), float(p["output"]))
    return prices


PRICES = _load_prices()


def price_for(model: str | None) -> Price | None:
    """Look up a model, tolerating ``models/`` prefixes and dated suffixes."""
    if not model:
        return None
    name = model.removeprefix("models/")
    if name in PRICES:
        return PRICES[name]
    # e.g. "gemini-3.1-flash-lite-001" -> longest known prefix
    matches = [k for k in PRICES if name.startswith(k)]
    return PRICES[max(matches, key=len)] if matches else None


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float:
    """Cost of one call; 0.0 for unknown models (and flagged by ``price_for`` returning None)."""
    p = price_for(model)
    if p is None:
        return 0.0
    return (input_tokens * p.input + output_tokens * p.output) / 1_000_000
