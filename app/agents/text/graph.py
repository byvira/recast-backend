"""
Text agent graph — single platform generation flow.
Compiled once at module load in orchestrator.py and reused for every request.

Flow:
  normalise → build_context → generate → generate_hooks → generate_seo
           → quality_check
           → passed  → collect_output → END
           → retry   → rewrite → quality_check
           → flag    → flag_review → collect_output → END
"""

from langgraph.graph import StateGraph, END
from app.agents.text.state import TextAgentState
from app.agents.text import nodes


def build_single_platform_graph():
    """
    Builds and compiles the text generation graph for a single platform.
    Node names are prefixed to avoid collision with TextAgentState field names.
    LangGraph raises ValueError if a node name matches a state key name.
    """
    graph = StateGraph(TextAgentState)

    # Register all nodes — names must not match any TextAgentState field name
    graph.add_node("normalise",       nodes.normalise_node)
    graph.add_node("build_context",   nodes.build_context_node)
    graph.add_node("generate",        nodes.generate_node)
    graph.add_node("generate_hooks",  nodes.hooks_node)       # "hooks" is a state key — renamed
    graph.add_node("generate_seo",    nodes.seo_node)         # "seo" could conflict — renamed
    graph.add_node("quality_check",   nodes.quality_check_node)
    graph.add_node("rewrite",         nodes.rewrite_node)
    graph.add_node("flag_review",     nodes.flag_node)        # "flag" is safe but renamed for clarity
    graph.add_node("collect_output",  nodes.collect_output_node)

    # Entry point
    graph.set_entry_point("normalise")

    # Linear edges
    graph.add_edge("normalise",      "build_context")
    graph.add_edge("build_context",  "generate")
    graph.add_edge("generate",       "generate_hooks")
    graph.add_edge("generate_hooks", "generate_seo")
    graph.add_edge("generate_seo",   "quality_check")

    # Conditional routing after quality check
    graph.add_conditional_edges(
        "quality_check",
        nodes.route_after_quality,
        {
            "passed": "collect_output",
            "retry":  "rewrite",
            "flag":   "flag_review",
        }
    )

    # Rewrite loops back to quality_check — one retry maximum
    graph.add_edge("rewrite",        "quality_check")
    graph.add_edge("flag_review",    "collect_output")
    graph.add_edge("collect_output", END)

    return graph.compile()