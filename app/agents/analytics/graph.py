"""
Analytics agent graph.
Compiled once at module load and reused for every request.

Flow:
  check_platforms → fetch_metrics → analyze → recommend → format_report → END
"""

from langgraph.graph import StateGraph, END
from app.agents.analytics.state import AnalyticsAgentState, build_initial_state
from app.agents.analytics import nodes
from app.core.tracing import ainvoke_traced
from app.db.mongo import users


def build_analytics_graph():
    """
    Builds and compiles the analytics agent graph.
    Linear flow — no conditional routing needed.
    """
    graph = StateGraph(AnalyticsAgentState)

    graph.add_node("check_platforms", nodes.check_platforms_node)
    graph.add_node("fetch_metrics",   nodes.fetch_metrics_node)
    graph.add_node("analyze",         nodes.analyze_node)
    graph.add_node("recommend",       nodes.recommend_node)
    graph.add_node("format_report",   nodes.format_report_node)

    graph.set_entry_point("check_platforms")

    graph.add_edge("check_platforms", "fetch_metrics")
    graph.add_edge("fetch_metrics",   "analyze")
    graph.add_edge("analyze",         "recommend")
    graph.add_edge("recommend",       "format_report")
    graph.add_edge("format_report",   END)

    return graph.compile()


# Compiled once at module load — reused for every request
analytics_graph = build_analytics_graph()


async def run_analytics(*, workspace_id: str, question: str, user_id: str = "") -> dict:
    """Run the analytics agent for one workspace, traced to LangSmith.

    Tags: ``agent:analytics``, ``ws:<workspace_id>``; ``user_id`` + ``question``
    in metadata. Returns the final graph state.

    ``language`` isn't threaded from the API layer (api/v1/analytics.py is
    out of this session's scope) — resolved here instead from the calling
    user's own ``users.language``, self-contained, same pattern used for
    score_hook/align_draft elsewhere in this i18n work.
    """
    language = "en"
    if user_id:
        try:
            doc = await users.find_one({"id": user_id}, {"language": 1})
            language = (doc or {}).get("language") or "en"
        except Exception:  # noqa: BLE001
            pass
    state = build_initial_state(workspace_id=workspace_id, question=question, user_id=user_id, language=language)
    result, _url = await ainvoke_traced(
        analytics_graph,
        state,
        run_name="analytics_pass",
        agent="analytics",
        workspace_id=workspace_id,
        user_id=user_id or None,
        metadata={"question": question},
    )
    return result