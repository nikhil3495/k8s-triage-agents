"""Session-state keys shared by agents, tools, callbacks and the API.

Everything stored under these keys is persisted by the ADK session service, which
is what makes investigations resumable.
"""

NAMESPACE = "namespace"
SYMPTOM = "symptom"
SCENARIO = "scenario"
CLUSTER_MODE = "cluster_mode"
TOOL_CALLS = "tool_calls"
LOG_FINDINGS = "log_findings"
METRICS_FINDINGS = "metrics_findings"
RUNBOOK_FINDINGS = "runbook_findings"
HYPOTHESIS = "hypothesis"
CRITIC_VERDICT = "critic_verdict"
CRITIC_HISTORY = "critic_history"
CRITIC_ROUND = "critic_round"
CHECKPOINTS = "checkpoints"
PROPOSED_ACTIONS = "proposed_action_ids"
