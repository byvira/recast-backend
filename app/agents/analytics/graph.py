"""
Analytics agent graph.
Compiled once at module load and reused for every request.

Flow:
  check_platforms → fetch_metrics → analyze → recommend → format_report → END
"""

from langgraph.graph import StateGraph, END
from app.agents.analytics.state import AnalyticsAgentState
from app.agents.analytics import nodes


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