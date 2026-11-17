"""Entry point for ``adk web adk_agents`` / ``adk run adk_agents/k8s_triage``.

Exposes the same agent tree the API runs, so every agent, LLM call and tool
call can be stepped through in the ADK dev UI. Set initial session state
(namespace, scenario, cluster_mode) in the dev UI, or rely on the defaults:
replay mode with DEFAULT_SCENARIO.
"""

from triage.agents.factory import build_root_agent
from triage.config import get_settings

root_agent = build_root_agent(get_settings())
