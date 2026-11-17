"""ADK plugin that adds granular spans and usage/cost accounting to every run.

ADK already emits its own ``invoke_agent`` / ``call_llm`` / ``execute_tool`` spans
(GenAI semantic conventions). This plugin adds our own spans with the attributes
we query on in Cloud Trace (``agent.name``, ``llm.model``, ``llm.input_tokens``,
``llm.output_tokens``, ``llm.tokens_per_sec``, ``llm.cost_usd``, ``tool.name``,
``latency_ms``) and accumulates per-invocation usage so the API can return the
cost of each request.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from triage.telemetry import metrics
from triage.telemetry.pricing import cost_usd

tracer = trace.get_tracer("triage.agents")


@dataclass
class AgentUsage:
    """Token/cost/latency totals for one agent."""

    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0

    @property
    def tokens_per_sec(self) -> float | None:
        """Output tokens per second of LLM time."""
        if self.llm_latency_ms <= 0:
            return None
        return round(self.output_tokens / (self.llm_latency_ms / 1000), 2)


@dataclass
class InvocationUsage:
    """Usage for one ADK invocation, broken down by agent."""

    by_agent: dict[str, AgentUsage] = field(default_factory=dict)

    def agent(self, name: str) -> AgentUsage:
        """Get-or-create the bucket for ``name``."""
        return self.by_agent.setdefault(name, AgentUsage())

    def merge(self, other: InvocationUsage) -> None:
        """Add another invocation's usage into this one (used across resumes)."""
        for name, u in other.by_agent.items():
            mine = self.agent(name)
            mine.llm_calls += u.llm_calls
            mine.tool_calls += u.tool_calls
            mine.input_tokens += u.input_tokens
            mine.output_tokens += u.output_tokens
            mine.cost_usd += u.cost_usd
            mine.llm_latency_ms += u.llm_latency_ms

    @property
    def totals(self) -> dict[str, Any]:
        """Aggregate over all agents."""
        return {
            "llm_calls": sum(u.llm_calls for u in self.by_agent.values()),
            "tool_calls": sum(u.tool_calls for u in self.by_agent.values()),
            "input_tokens": sum(u.input_tokens for u in self.by_agent.values()),
            "output_tokens": sum(u.output_tokens for u in self.by_agent.values()),
            "cost_usd": round(sum(u.cost_usd for u in self.by_agent.values()), 6),
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation for the API."""
        return {
            "totals": self.totals,
            "by_agent": {
                n: {
                    "llm_calls": u.llm_calls,
                    "tool_calls": u.tool_calls,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cost_usd": round(u.cost_usd, 6),
                    "llm_latency_ms": round(u.llm_latency_ms, 1),
                    "tokens_per_sec": u.tokens_per_sec,
                }
                for n, u in self.by_agent.items()
            },
        }


#: Ledger for the investigation currently running in this asyncio task. AgentTool
#: runs sub-agents in nested runners with their own invocation ids; the context
#: variable lets their usage roll up into the parent investigation.
CURRENT_LEDGER: ContextVar[InvocationUsage | None] = ContextVar("triage_ledger", default=None)

StepSink = Callable[[dict[str, Any]], None]
#: Receives a dict per agent step (LLM turn or tool call) for live streaming / persistence.
CURRENT_STEP_SINK: ContextVar[StepSink | None] = ContextVar("triage_step_sink", default=None)


def _emit(step: dict[str, Any]) -> None:
    sink = CURRENT_STEP_SINK.get()
    if sink is not None:
        sink(step)


def _preview(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


@dataclass
class _Open:
    span: Span
    start: float
    model: str = ""


class TelemetryPlugin(BasePlugin):
    """Spans + usage accounting for agents, LLM calls and tools."""

    def __init__(self) -> None:
        """Create an empty usage ledger."""
        super().__init__(name="triage_telemetry")
        self._usage: dict[str, InvocationUsage] = {}
        self._agents: dict[tuple[str, str], _Open] = {}
        self._llm: dict[tuple[str, str], _Open] = {}
        self._tools: dict[str, _Open] = {}

    # ------------------------------------------------------------- ledger API
    def usage_for(self, invocation_id: str) -> InvocationUsage:
        """Return the active investigation ledger, else usage for ``invocation_id``."""
        ledger = CURRENT_LEDGER.get()
        if ledger is not None:
            return ledger
        return self._usage.setdefault(invocation_id, InvocationUsage())

    def pop_usage(self, invocation_id: str) -> InvocationUsage:
        """Remove and return an invocation's usage."""
        return self._usage.pop(invocation_id, InvocationUsage())

    # ----------------------------------------------------------------- agents
    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        """Open an ``agent:<name>`` span."""
        span = tracer.start_span(
            f"agent:{agent.name}",
            attributes={"agent.name": agent.name, "invocation.id": callback_context.invocation_id},
        )
        self._agents[(callback_context.invocation_id, agent.name)] = _Open(
            span, time.perf_counter()
        )
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        """Close the agent span with its usage totals."""
        op = self._agents.pop((callback_context.invocation_id, agent.name), None)
        if op:
            u = self.usage_for(callback_context.invocation_id).agent(agent.name)
            op.span.set_attributes(
                {
                    "latency_ms": round((time.perf_counter() - op.start) * 1000, 1),
                    "llm.input_tokens": u.input_tokens,
                    "llm.output_tokens": u.output_tokens,
                    "llm.cost_usd": round(u.cost_usd, 6),
                    "agent.tool_calls": u.tool_calls,
                }
            )
            op.span.end()
        return None

    # -------------------------------------------------------------------- llm
    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        """Open an ``llm:<model>`` span."""
        model = llm_request.model or ""
        span = tracer.start_span(
            f"llm:{model}",
            attributes={"agent.name": callback_context.agent_name, "llm.model": model},
        )
        key = (callback_context.invocation_id, callback_context.agent_name)
        self._llm[key] = _Open(span, time.perf_counter(), model)
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        """Record tokens, tokens/sec and cost for the call."""
        if llm_response.partial:
            return None
        key = (callback_context.invocation_id, callback_context.agent_name)
        op = self._llm.pop(key, None)
        if op is None:
            return None
        latency_ms = (time.perf_counter() - op.start) * 1000
        um = llm_response.usage_metadata
        in_tok = int((um.prompt_token_count if um else 0) or 0)
        out_tok = int(
            ((um.candidates_token_count if um else 0) or 0)
            + ((um.thoughts_token_count if um else 0) or 0)
        )
        cost = cost_usd(op.model, in_tok, out_tok)
        tps = round(out_tok / (latency_ms / 1000), 2) if latency_ms > 0 else 0.0
        op.span.set_attributes(
            {
                "llm.input_tokens": in_tok,
                "llm.output_tokens": out_tok,
                "llm.tokens_per_sec": tps,
                "llm.cost_usd": round(cost, 8),
                "latency_ms": round(latency_ms, 1),
            }
        )
        if llm_response.error_code:
            op.span.set_status(Status(StatusCode.ERROR, str(llm_response.error_message)))
        op.span.end()
        u = self.usage_for(callback_context.invocation_id).agent(callback_context.agent_name)
        u.llm_calls += 1
        u.input_tokens += in_tok
        u.output_tokens += out_tok
        u.cost_usd += cost
        u.llm_latency_ms += latency_ms
        metrics.record_tokens_per_sec(callback_context.agent_name, op.model, tps)
        parts = (llm_response.content.parts if llm_response.content else None) or []
        calls = [p.function_call.name for p in parts if p.function_call]
        text = " ".join(p.text for p in parts if p.text and not p.thought)
        _emit(
            {
                "kind": "llm",
                "agent": callback_context.agent_name,
                "model": op.model,
                "calls": calls,
                "text": _preview(text) if text else "",
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "tokens_per_sec": tps,
                "cost_usd": round(cost, 8),
                "latency_ms": round(latency_ms, 1),
            }
        )
        return None

    async def on_model_error_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
    ) -> LlmResponse | None:
        """Close the LLM span as an error."""
        op = self._llm.pop((callback_context.invocation_id, callback_context.agent_name), None)
        if op:
            op.span.record_exception(error)
            op.span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            op.span.end()
        return None

    # ------------------------------------------------------------------ tools
    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        """Open a ``tool:<name>`` span."""
        span = tracer.start_span(
            f"tool:{tool.name}",
            attributes={"tool.name": tool.name, "agent.name": tool_context.agent_name},
        )
        self._tools[tool_context.function_call_id or tool.name] = _Open(span, time.perf_counter())
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Close the tool span; mark error observations."""
        op = self._tools.pop(tool_context.function_call_id or tool.name, None)
        if op:
            op.span.set_attribute("latency_ms", round((time.perf_counter() - op.start) * 1000, 1))
            if isinstance(result, dict) and "error" in result:
                op.span.set_attribute("tool.error", str(result["error"])[:500])
            op.span.end()
        self.usage_for(tool_context.invocation_id).agent(tool_context.agent_name).tool_calls += 1
        _emit(
            {
                "kind": "tool",
                "agent": tool_context.agent_name,
                "tool": tool.name,
                "args": tool_args,
                "result": _preview(result),
                "latency_ms": None
                if op is None
                else round((time.perf_counter() - op.start) * 1000, 1),
            }
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        """Close the tool span as an error."""
        op = self._tools.pop(tool_context.function_call_id or tool.name, None)
        if op:
            op.span.record_exception(error)
            op.span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            op.span.end()
        return None
