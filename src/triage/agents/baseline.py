"""A deterministic, rule-based stand-in for Gemini ("baseline" model).

It drives the *real* agent graph and tools - it reads the actual tool
observations from the replay fixtures and decides the next tool call / answer
with hand-written rules. It exists so CI can run the full eval pipeline on
every PR without an API key or LLM cost. Its scores measure the plumbing,
fixtures and retrieval, NOT Gemini's reasoning; the README reports them
separately from live Gemini runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

MARKERS = {
    "You are LogAgent": "LogAgent",
    "You are MetricsAgent": "MetricsAgent",
    "You are RunbookAgent": "RunbookAgent",
    "You are the incident Supervisor": "Supervisor",
    "You are the Critic": "Critic",
}

# (category, regex over all observations, query for the runbook search)
RULES: list[tuple[str, str, str]] = [
    ("image_pull", r"ImagePullBackOff|ErrImagePull", "ImagePullBackOff Failed to pull image"),
    ("oom", r"OOMKilled", "OOMKilled exit code 137 memory limit"),
    (
        "missing_config",
        r"CreateContainerConfigError|configmap \\?\"[^\"]+\\?\" not found",
        "CreateContainerConfigError configmap not found",
    ),
    (
        "unschedulable",
        r"FailedScheduling|Insufficient (cpu|memory)",
        "FailedScheduling Insufficient cpu Pending",
    ),
    ("readiness_probe", r"Readiness probe failed", "Readiness probe failed HTTP probe 404"),
    (
        "crashloop",
        r"CrashLoopBackOff|Back-off restarting failed container",
        "CrashLoopBackOff exit code 1 Back-off restarting failed container",
    ),
]
EVIDENCE_PATTERNS = [
    r"Back-off pulling image [^\"\\]*\\?\"[^\"\\]+",
    r"Failed to pull image \\?\"[^\"\\]+",
    r"ImagePullBackOff|ErrImagePull",
    r"OOMKilled",
    r"\"exit_code\": ?\d+",
    r"\"memory\": ?\"\d+Mi\"",
    r"CreateContainerConfigError",
    r"configmap \\?\"[^\"\\]+\\?\" not found",
    r"0/\d+ nodes are available[^\"\\]*",
    r"Insufficient cpu",
    r"Readiness probe failed: [^\"\\]*",
    r"FATAL[^\"\\]*",
    r"Back-off restarting failed container[^\"\\]*",
    r"\"cpu_pct_of_limit\": ?[\d.]+",
    r"\"cpu\": ?\"\d+m\"",
]


def _agent(req: LlmRequest) -> str:
    si = req.config.system_instruction if req.config else None
    text = si if isinstance(si, str) else json.dumps(str(si))
    for marker, name in MARKERS.items():
        if marker in text:
            return name
    return "unknown"


def _observations(req: LlmRequest) -> list[tuple[str, Any]]:
    """(tool name, response) pairs since the agent's latest text prompt."""
    out: list[tuple[str, Any]] = []
    for content in req.contents:
        parts = content.parts or []
        if (
            content.role == "user"
            and any(p.text for p in parts)
            and not any(p.function_response for p in parts)
        ):
            out = []
        for p in parts:
            if p.function_response:
                out.append((p.function_response.name or "", p.function_response.response))
    return out


def _request_text(req: LlmRequest) -> str:
    for content in reversed(req.contents):
        if content.role == "user":
            text = " ".join(p.text or "" for p in content.parts or [] if p.text)
            if text:
                return text
    return ""


def _call(name: str, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def _text(s: str) -> types.Part:
    return types.Part(text=s)


def _blob(obs: list[tuple[str, Any]]) -> str:
    return json.dumps([o for _, o in obs], default=str)


def _unescape(blob: str) -> str:
    # Worker findings arrive as JSON nested inside JSON strings.
    while '\\"' in blob:
        blob = blob.replace('\\"', '"')
    return blob


def classify(blob: str) -> tuple[str, str]:
    """Return (category, runbook query) for the observed evidence."""
    blob = _unescape(blob)
    for category, rx, query in RULES:
        if re.search(rx, blob):
            return category, query
    m = re.search(r'"cpu_pct_of_limit": ?([\d.]+)', blob)
    if m and float(m.group(1)) >= 90:
        return "cpu_throttling", "CPU throttling high latency cpu limit"
    if re.search(r'"ready_endpoints": ?0', blob) and re.search(
        r'"pods_matching_selector": ?0', blob
    ):
        return "service_selector", "Service has no endpoints selector does not match pod labels"
    return "unknown", "pod not healthy"


def _evidence(blob: str, category: str) -> list[dict[str, str]]:
    blob = _unescape(blob)
    found: list[dict[str, str]] = []
    for rx in EVIDENCE_PATTERNS:
        for m in re.finditer(rx, blob):
            snippet = m.group(0).replace('\\"', '"')
            if snippet not in [e["snippet"] for e in found]:
                found.append({"source": "cluster", "snippet": snippet})
            if len(found) >= 6:
                return found
    if category == "service_selector":
        for m in re.finditer(r'"selector": ?\{[^}]*\}', blob):
            found.append({"source": "services", "snippet": m.group(0)})
        found.append({"source": "services", "snippet": "ready_endpoints: 0"})
    return found[:8]


def _findings(obs: list[tuple[str, Any]]) -> list[Any]:
    """Parse the JSON observations embedded in worker findings."""
    out: list[Any] = []
    for name, resp in obs:
        if name not in ("LogAgent", "MetricsAgent"):
            continue
        text = resp.get("result", "") if isinstance(resp, dict) else str(resp)
        body = str(text).split("FINDINGS:\n", 1)[-1]
        try:
            out.extend(json.loads(body))
        except ValueError:
            continue
    return out


def _walk(obj: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            found.extend(_walk(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_walk(v, key))
    return found


def _remediation_for(category: str, findings: list[Any]) -> dict[str, Any] | None:
    """Map a category to one of the allowed actions (always gated by human approval)."""
    deployments = [d for ds in _walk(findings, "deployments") for d in ds]
    containers = [c for c in _walk(findings, "container") if isinstance(c, str)]
    if not deployments:
        return None
    dep = str(deployments[0].get("name"))
    container = containers[0] if containers else "app"
    if category in ("image_pull", "crashloop", "readiness_probe"):
        return {
            "action": "rollback_deployment",
            "deployment": dep,
            "rationale": f"{category} started with the latest rollout; roll back to the "
            "previous ReplicaSet revision.",
        }
    if category == "oom":
        return {
            "action": "patch_resources",
            "deployment": dep,
            "container": container,
            "memory_limit": "256Mi",
            "rationale": "Container is OOMKilled at its memory limit; raise the limit.",
        }
    if category == "cpu_throttling":
        return {
            "action": "patch_resources",
            "deployment": dep,
            "container": container,
            "cpu_limit": "500m",
            "rationale": "Container is pinned at its CPU limit; raise the limit.",
        }
    if category == "unschedulable":
        return {
            "action": "patch_resources",
            "deployment": dep,
            "container": container,
            "cpu_request": "100m",
            "rationale": "CPU request exceeds any node's allocatable CPU; lower it.",
        }
    return None


class BaselineLlm(BaseLlm):
    """Rule-based policy that plays every agent role."""

    model: str = "rule-based-baseline"

    @property
    def capabilities(self) -> LlmCapabilities:
        """Declare structured output + tools support (final answer is JSON)."""
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Return the next tool call or answer for whichever agent is asking."""
        agent = _agent(llm_request)
        obs = _observations(llm_request)
        part = getattr(self, f"_{agent.lower()}")(llm_request, obs)
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=0, candidates_token_count=0, total_token_count=0
            ),
        )

    # -------------------------------------------------------------- workers
    def _ns(self, req: LlmRequest) -> str:
        si = str(req.config.system_instruction) if req.config else ""
        m = re.search(r'namespace "([^"]+)"', si) or re.search(
            r"namespace '?([a-z0-9-]+)'?", _request_text(req)
        )
        return m.group(1) if m else "default"

    def _logagent(self, req: LlmRequest, obs: list[tuple[str, Any]]) -> types.Part:
        ns = self._ns(req)
        done = [n for n, _ in obs]
        pods = next((o for n, o in obs if n == "list_pods"), None)
        if pods is None:
            return _call("list_pods", namespace=ns)
        plist = pods.get("pods", []) if isinstance(pods, dict) else []
        bad = sorted(
            plist, key=lambda p: (p.get("ready", "0/1").split("/")[0] != "0", -p.get("restarts", 0))
        )
        pod = bad[0]["name"] if bad else ""
        if pod and "describe_pod" not in done:
            return _call("describe_pod", namespace=ns, pod_name=pod)
        if "get_events" not in done:
            return _call("get_events", namespace=ns, object_name="")
        if pod and "get_pod_logs" not in done:
            return _call(
                "get_pod_logs", namespace=ns, pod_name=pod, previous=bool(bad[0].get("restarts"))
            )
        if "list_services" not in done:
            return _call("list_services", namespace=ns)
        return _text("FINDINGS:\n" + _blob(obs)[:30000])

    def _metricsagent(self, req: LlmRequest, obs: list[tuple[str, Any]]) -> types.Part:
        ns = self._ns(req)
        done = [n for n, _ in obs]
        for tool in ("get_pod_resource_usage", "get_restart_counts", "list_deployments"):
            if tool not in done:
                return _call(tool, namespace=ns)
        return _text("FINDINGS:\n" + _blob(obs)[:30000])

    def _runbookagent(self, req: LlmRequest, obs: list[tuple[str, Any]]) -> types.Part:
        if not obs:
            return _call("search_runbooks", query=_request_text(req)[:300])
        results = obs[-1][1].get("results", []) if isinstance(obs[-1][1], dict) else []
        ids: list[str] = []
        for r in results:
            if r["runbook_id"] not in ids:
                ids.append(r["runbook_id"])
        return _text("RUNBOOKS: " + ", ".join(ids[:3]))

    # ----------------------------------------------------------- supervisor
    def _supervisor(self, req: LlmRequest, obs: list[tuple[str, Any]]) -> types.Part:
        done = [n for n, _ in obs]
        ns = self._ns(req)
        if "LogAgent" not in done:
            return _call(
                "LogAgent",
                request=f"In namespace '{ns}': investigate failing pods, events, logs, services.",
            )
        if "MetricsAgent" not in done:
            return _call(
                "MetricsAgent",
                request=f"In namespace '{ns}': check usage vs limits, restarts, deployments.",
            )
        blob = _blob([o for o in obs if o[0] in ("LogAgent", "MetricsAgent")])
        category, query = classify(blob)
        if "RunbookAgent" not in done:
            return _call("RunbookAgent", request=query)
        rb = str(next(o for n, o in obs if n == "RunbookAgent"))
        refs = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", rb.split("RUNBOOKS:", 1)[-1])[:3]
        evidence = _evidence(blob, category)
        remediation = _remediation_for(category, _findings(obs))
        proposals: list[dict[str, Any]] = []
        if remediation is not None:
            if "propose_remediation" not in done:
                return _call("propose_remediation", **remediation)
            result = next(o for n, o in obs if n == "propose_remediation")
            proposals.append(
                {
                    "action": remediation["action"],
                    "deployment": remediation["deployment"],
                    "rationale": remediation["rationale"],
                    "pending_action_id": (result or {}).get("pending_action_id"),
                }
            )
        elif category in ("missing_config", "service_selector"):
            proposals.append(
                {
                    "action": "manual",
                    "rationale": "create the missing object / fix the Service selector "
                    "(outside the allowed automated actions)",
                }
            )
        return _text(
            json.dumps(
                {
                    "summary": f"Rule-based baseline classified the incident as {category}.",
                    "root_cause": f"{category}: " + "; ".join(e["snippet"] for e in evidence[:3]),
                    "category": category,
                    "confidence": 0.7 if category != "unknown" else 0.2,
                    "evidence": evidence,
                    "proposed_remediations": proposals,
                    "runbook_refs": refs,
                }
            )
        )

    def _critic(self, req: LlmRequest, obs: list[tuple[str, Any]]) -> types.Part:
        si = str(req.config.system_instruction) if req.config else ""
        ok = '"category": "unknown"' not in si and '"evidence": []' not in si
        return _text(
            json.dumps(
                {
                    "verdict": "PASS" if ok else "REVISE",
                    "reasons": ["evidence present" if ok else "no category/evidence"],
                    "missing_evidence": [],
                }
            )
        )
