from langgraph.graph import StateGraph, END
from app.agents.base import BaseAgentState, avg_quality
from app.agents.audio import nodes


def build_audio_agent():
    """Build and compile the audio pipeline LangGraph agent."""
    graph = StateGraph(BaseAgentState)

    graph.add_node("plan",       nodes.plan_node)
    graph.add_node("transcribe", nodes.transcribe_node)
    graph.add_node("analyse",    nodes.analyse_node)
    graph.add_node("generate",   nodes.generate_node)
    graph.add_node("evaluate",   nodes.evaluate_node)
    graph.add_node("retry",      nodes.retry_node)
    graph.add_node("deliver",    nodes.deliver_node)

    graph.set_entry_point("plan")

    graph.add_edge("plan",       "transcribe")
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


audio_agent = build_audio_agent()
