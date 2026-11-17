"""RunbookAgent's retrieval tool."""

from __future__ import annotations

from google.adk.tools.tool_context import ToolContext

from triage.k8s.base import JSON


def search_runbooks(query: str, tool_context: ToolContext) -> JSON:
    """Search the internal runbook library (hybrid BM25 + embedding search, re-ranked).

    Args:
        query: What to look for - exact error strings work best, e.g. "OOMKilled exit code 137".

    """
    from triage.runtime import get_runtime

    retriever = get_runtime().retriever
    results = retriever.search(query, k=5)
    cited = list(tool_context.state.get("runbook_citations") or [])
    for r in results:
        if r.chunk_id not in cited:
            cited.append(r.chunk_id)
    tool_context.state["runbook_citations"] = cited
    return {
        "retrieval_mode": retriever.mode,
        "results": [r.model_dump(exclude={"bm25_rank", "dense_rank"}) for r in results],
    }
