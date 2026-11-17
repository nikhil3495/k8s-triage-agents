"""Per-step checkpoints and guardrails, implemented as ADK agent/tool callbacks.

ADK persists every event (and its state delta) through the session service as it
is yielded, so the event log *is* the durable checkpoint. These callbacks add an
explicit, queryable checkpoint record after each sub-agent step and enforce the
per-agent ReAct tool-call budget.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from triage import state_keys


def record_checkpoint(callback_context: CallbackContext) -> types.Content | None:
    """after_agent_callback: append ``{agent, at, invocation_id}`` to the checkpoint list."""
    cps = list(callback_context.state.get(state_keys.CHECKPOINTS) or [])
    cps.append(
        {
            "agent": callback_context.agent_name,
            "invocation_id": callback_context.invocation_id,
            "at": datetime.now(UTC).isoformat(),
        }
    )
    callback_context.state[state_keys.CHECKPOINTS] = cps
    return None


def make_tool_budget(limit: int) -> Any:
    """before_tool_callback enforcing at most ``limit`` tool calls per agent per invocation."""

    def enforce_budget(
        tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        counts: dict[str, int] = dict(tool_context.state.get(state_keys.TOOL_CALLS) or {})
        key = f"{tool_context.invocation_id}:{tool_context.agent_name}"
        used = counts.get(key, 0)
        if used >= limit:
            return {
                "error": f"tool-call budget of {limit} exhausted for {tool_context.agent_name}; "
                "stop calling tools and give your final answer now."
            }
        counts[key] = used + 1
        tool_context.state[state_keys.TOOL_CALLS] = counts
        return None

    return enforce_budget


def record_critic_verdict(callback_context: CallbackContext) -> types.Content | None:
    """after_agent_callback for the critic: keep every verdict in session history."""
    verdict = callback_context.state.get(state_keys.CRITIC_VERDICT)
    history = list(callback_context.state.get(state_keys.CRITIC_HISTORY) or [])
    rnd = int(callback_context.state.get(state_keys.CRITIC_ROUND) or 0) + 1
    if isinstance(verdict, dict):
        history.append({"round": rnd, **verdict})
    callback_context.state[state_keys.CRITIC_HISTORY] = history
    callback_context.state[state_keys.CRITIC_ROUND] = rnd
    return record_checkpoint(callback_context)
