"""Builds the agent hierarchy.

::

    TriageLoop (LoopAgent, max_iterations = MAX_CRITIC_ROUNDS)
     ├── Supervisor (LlmAgent, output_schema=RootCauseHypothesis)
     │     tools: AgentTool(LogAgent), AgentTool(MetricsAgent), AgentTool(RunbookAgent),
     │            propose_remediation (files a HITL request only)
     ├── Critic (LlmAgent, output_schema=CriticVerdict, read-only verification tools)
     └── VerdictGate (BaseAgent) - escalates (ends the loop) on PASS; on REVISE the loop
                                   runs the Supervisor again with the critic's feedback

Each worker is a ReAct agent (ADK ``PlanReActPlanner``) with a per-run tool-call
budget enforced in ``before_tool_callback``; the run-wide LLM-call cap is
``RunConfig.max_llm_calls``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event, EventActions
from google.adk.models.base_llm import BaseLlm
from google.adk.planners import PlanReActPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

from triage import state_keys
from triage.agents import prompts
from triage.agents.schemas import CriticVerdict, RootCauseHypothesis
from triage.config import Settings
from triage.sessions.checkpoints import make_tool_budget, record_checkpoint, record_critic_verdict
from triage.telemetry import metrics
from triage.tools.cluster_tools import (
    LOG_TOOLS,
    METRICS_TOOLS,
    describe_pod,
    get_events,
    get_pod_logs,
)
from triage.tools.remediation_tools import propose_remediation
from triage.tools.runbook_tools import search_runbooks

ModelRef = str | BaseLlm

LOG_AGENT = "LogAgent"
METRICS_AGENT = "MetricsAgent"
RUNBOOK_AGENT = "RunbookAgent"
SUPERVISOR = "Supervisor"
CRITIC = "Critic"
GATE = "VerdictGate"
ROOT = "TriageLoop"


@dataclass
class Models:
    """Model per role; strings are Gemini model ids, or inject a BaseLlm (tests)."""

    supervisor: ModelRef
    worker: ModelRef
    critic: ModelRef

    @classmethod
    def from_settings(cls, s: Settings) -> Models:
        """Read SUPERVISOR_MODEL / WORKER_MODEL / CRITIC_MODEL."""
        return cls(s.supervisor_model, s.worker_model, s.critic_model)


class VerdictGate(BaseAgent):
    """Ends the critic loop when the latest verdict is PASS."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        verdict = ctx.session.state.get(state_keys.CRITIC_VERDICT) or {}
        decision = str(verdict.get("verdict", "REVISE")) if isinstance(verdict, dict) else "REVISE"
        metrics.record_critic_verdict(decision)
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            branch=ctx.branch,
            content=types.Content(
                role="model", parts=[types.Part(text=f"critic verdict: {decision}")]
            ),
            actions=EventActions(escalate=decision == "PASS"),
        )


def _worker(
    name: str,
    model: ModelRef,
    instruction: str,
    tools: list[Callable[..., Any]],
    budget: int,
    description: str,
) -> LlmAgent:
    return LlmAgent(
        name=name,
        model=model,
        description=description,
        instruction=instruction,
        tools=[FunctionTool(t) for t in tools],
        planner=PlanReActPlanner(),
        before_tool_callback=make_tool_budget(budget),
        after_agent_callback=record_checkpoint,
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
    )


def _critic_instruction(ctx: ReadonlyContext) -> str:
    import json

    hyp = ctx.state.get(state_keys.HYPOTHESIS)
    return (
        f"{prompts.CRITIC}\n\nNamespace: {ctx.state.get(state_keys.NAMESPACE)}\n"
        f"Reported symptom: {ctx.state.get(state_keys.SYMPTOM)}\n"
        f"Hypothesis under review:\n{json.dumps(hyp, indent=1, default=str)}"
    )


def build_root_agent(settings: Settings, models: Models | None = None) -> LoopAgent:
    """Construct the full hierarchy from settings (models injectable for tests)."""
    m = models or Models.from_settings(settings)
    budget = settings.max_tool_calls_per_agent
    log_agent = _worker(
        LOG_AGENT,
        m.worker,
        prompts.LOG_AGENT,
        LOG_TOOLS,
        budget,
        "Investigates pod status, describe output, events, logs and service endpoints.",
    )
    metrics_agent = _worker(
        METRICS_AGENT,
        m.worker,
        prompts.METRICS_AGENT,
        METRICS_TOOLS,
        budget,
        "Investigates resource usage vs limits, restarts/exit codes, deployments and nodes.",
    )
    runbook_agent = _worker(
        RUNBOOK_AGENT,
        m.worker,
        prompts.RUNBOOK_AGENT,
        [search_runbooks],
        3,
        "Retrieves and cites the internal runbooks that match an incident.",
    )
    supervisor = LlmAgent(
        name=SUPERVISOR,
        model=m.supervisor,
        description="Plans the investigation, delegates to specialists, writes the hypothesis.",
        instruction=prompts.supervisor_instruction,
        tools=[
            AgentTool(log_agent),
            AgentTool(metrics_agent),
            AgentTool(runbook_agent),
            FunctionTool(propose_remediation),
        ],
        output_schema=RootCauseHypothesis,
        output_key=state_keys.HYPOTHESIS,
        after_agent_callback=record_checkpoint,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
    )
    critic = LlmAgent(
        name=CRITIC,
        model=m.critic,
        description="Self-reflection: verifies the hypothesis against cluster evidence.",
        instruction=_critic_instruction,
        include_contents="none",
        tools=[FunctionTool(describe_pod), FunctionTool(get_events), FunctionTool(get_pod_logs)],
        before_tool_callback=make_tool_budget(3),
        output_schema=CriticVerdict,
        output_key=state_keys.CRITIC_VERDICT,
        after_agent_callback=record_critic_verdict,
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
    )
    return LoopAgent(
        name=ROOT,
        description="Supervisor -> Critic loop; ends on PASS or after max critic rounds.",
        sub_agents=[supervisor, critic, VerdictGate(name=GATE)],
        max_iterations=settings.max_critic_rounds,
    )
