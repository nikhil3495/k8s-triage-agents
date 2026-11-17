"""Deterministic fake LLM for driving the real ADK agent graph in tests."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

Step = tuple[str, Any] | tuple[str, str, dict[str, Any]]

AGENT_MARKERS = {
    "You are LogAgent": "LogAgent",
    "You are MetricsAgent": "MetricsAgent",
    "You are RunbookAgent": "RunbookAgent",
    "You are the incident Supervisor": "Supervisor",
    "You are the Critic": "Critic",
}


class SimulatedCrash(RuntimeError):
    """Raised by the fake model to simulate a process/model failure mid-run."""


def _system_text(req: LlmRequest) -> str:
    si = req.config.system_instruction if req.config else None
    if si is None:
        return ""
    if isinstance(si, str):
        return si
    if isinstance(si, types.Content):
        return " ".join(p.text or "" for p in si.parts or [])
    return str(si)


def agent_of(req: LlmRequest) -> str:
    text = _system_text(req)
    for marker, name in AGENT_MARKERS.items():
        if marker in text:
            return name
    raise AssertionError(f"cannot identify agent from instruction: {text[:120]!r}")


def step_index(req: LlmRequest) -> int:
    """Number of tool results the agent has seen since its latest text prompt."""
    n = 0
    for content in reversed(req.contents):
        parts = content.parts or []
        if any(p.function_response for p in parts):
            n += sum(1 for p in parts if p.function_response)
            continue
        if content.role == "user" and any(p.text for p in parts):
            break
    return n


class ScriptedLlm(BaseLlm):
    """Replays a per-agent script of tool calls / final answers.

    ``scripts[agent]`` is a list of steps:
    ``("call", tool_name, args)`` or ``("text", str | dict | Callable[[LlmRequest], str])``.
    ``crash_at`` = {(agent, step)} raises :class:`SimulatedCrash` the first time.
    """

    model: str = "gemini-3.1-flash-lite"
    scripts: dict[str, list[Any]]
    crash_at: set[tuple[str, int]] = set()
    calls: list[tuple[str, int]] = []
    tokens: tuple[int, int] = (1200, 150)

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        agent = agent_of(llm_request)
        idx = step_index(llm_request)
        self.calls.append((agent, idx))
        if (agent, idx) in self.crash_at:
            self.crash_at.discard((agent, idx))
            raise SimulatedCrash(f"simulated crash in {agent} at step {idx}")
        script = self.scripts[agent]
        step = script[min(idx, len(script) - 1)]
        if step[0] == "call":
            part = types.Part(function_call=types.FunctionCall(name=step[1], args=step[2]))
        else:
            payload = step[1]
            if callable(payload):
                payload = payload(llm_request)
            if isinstance(payload, dict):
                payload = json.dumps(payload)
            part = types.Part(text=payload)
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.tokens[0],
                candidates_token_count=self.tokens[1],
                total_token_count=sum(self.tokens),
            ),
            model_version=self.model,
        )


def triage_script(
    namespace: str,
    *,
    pod: str,
    hypothesis: dict[str, Any],
    critic: list[dict[str, Any]] | None = None,
    remediation: dict[str, Any] | None = None,
    runbook_query: str = "ImagePullBackOff",
) -> dict[str, list[Any]]:
    """A realistic happy-path script for the whole hierarchy."""
    sup: list[Any] = [
        ("call", "LogAgent", {"request": f"Investigate pods in {namespace}"}),
        ("call", "MetricsAgent", {"request": f"Check resources in {namespace}"}),
        ("call", "RunbookAgent", {"request": runbook_query}),
    ]
    if remediation:
        sup.append(("call", "propose_remediation", remediation))
    sup.append(("text", hypothesis))
    verdicts = critic or [{"verdict": "PASS", "reasons": ["evidence matches"]}]
    critic_steps: list[Any] = [
        ("call", "describe_pod", {"namespace": namespace, "pod_name": pod}),
        ("text", _verdict_seq(verdicts)),
    ]
    return {
        "LogAgent": [
            ("call", "list_pods", {"namespace": namespace}),
            ("call", "describe_pod", {"namespace": namespace, "pod_name": pod}),
            ("call", "get_events", {"namespace": namespace, "object_name": pod}),
            ("text", "FINDINGS: pod is failing. SUSPECTED CAUSE: see events."),
        ],
        "MetricsAgent": [
            ("call", "get_restart_counts", {"namespace": namespace}),
            ("text", "FINDINGS: restart counts collected."),
        ],
        "RunbookAgent": [
            ("call", "search_runbooks", {"query": runbook_query}),
            ("text", "imagepullbackoff - Diagnosis: matches"),
        ],
        "Supervisor": sup,
        "Critic": critic_steps,
    }


def _verdict_seq(verdicts: list[dict[str, Any]]) -> Callable[[LlmRequest], str]:
    state = {"i": 0}

    def next_verdict(_req: LlmRequest) -> str:
        v = verdicts[min(state["i"], len(verdicts) - 1)]
        state["i"] += 1
        return json.dumps(v)

    return next_verdict
