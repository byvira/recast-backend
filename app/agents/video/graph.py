from langgraph.graph import StateGraph, END
from app.agents.base import BaseAgentState, avg_quality
from app.agents.video import nodes


def build_video_agent():
    """Build and compile the video pipeline LangGraph agent."""
    graph = StateGraph(BaseAgentState)

    # "plan" collides with BaseAgentState's own `plan: list[str]` field —
    # LangGraph refuses a node name that's also a state key (the same
    # bug confirmed and fixed in app/agents/audio/graph.py 2026-09-26,
    # applied here the same way: this graph could not be imported before
    # this fix either).
    graph.add_node("plan_step",  nodes.plan_node)
    graph.add_node("transcribe", nodes.transcribe_node)
    graph.add_node("analyse",    nodes.analyse_node)
    graph.add_node("generate",   nodes.generate_node)
    graph.add_node("evaluate",   nodes.evaluate_node)
    graph.add_node("retry",      nodes.retry_node)
    graph.add_node("deliver",    nodes.deliver_node)

    graph.set_entry_point("plan_step")

    graph.add_edge("plan_step",  "transcribe")
    graph.add_edge("transcribe", "analyse")
    graph.add_edge("analyse",    "generate")
    graph.add_edge("generate",   "evaluate")
    graph.add_edge("retry",      "generate")
    graph.add_edge("deliver",    END)

    graph.add_conditional_edges(
        "evaluate",
        lambda s: "retry" if (
            avg_quality(s) < 0.75
            and s["retry_count"] < s["max_retries"]
        ) else "deliver",
    )

    return graph.compile()


video_agent = build_video_agent()
