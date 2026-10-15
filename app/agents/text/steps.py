"""The text agent's steps as registered tools on the shared runtime.

The graph in `graph.py` keeps running the session exactly as before; this registry names each of its steps, states what each one
costs, and lets a director (Remy, Odette, or another pipeline's agent) see and plan with the same vocabulary the image, audio and
video agents use. Each tool runs the graph's own node on a state dict and returns what that node writes, so there is one
implementation of every step.
"""
from __future__ import annotations

from typing import Any

from app.agents.runtime import ToolRegistry, ToolSpec
from app.agents.text import nodes

AGENT = "text"
REGISTRY = ToolRegistry(AGENT)


def _tool(node):
    async def run(*, state: dict) -> Any:
        return await node(state)

    return run


for _spec, _node in (
    (ToolSpec("normalise", "Clean and measure the source before writing", cost="free", timeout_s=30), nodes.normalise_node),
    (ToolSpec("build_context", "Gather the brand voice, rules and source into one context", cost="free", timeout_s=60), nodes.build_context_node),
    (ToolSpec("generate", "Write the post for a platform in the brand's voice", cost="llm", timeout_s=120), nodes.generate_node),
    (ToolSpec("generate_hooks", "Write alternative opening lines", cost="llm", timeout_s=90), nodes.hooks_node),
    (ToolSpec("generate_seo", "Write the search title, description and tags", cost="llm", timeout_s=90), nodes.seo_node),
    (ToolSpec("quality_check", "Check the draft against the voice, the rules and the platform", cost="llm", timeout_s=120), nodes.quality_check_node),
    (ToolSpec("rewrite", "Rewrite the draft with the specific issues as feedback", cost="llm", timeout_s=120), nodes.rewrite_node),
    (ToolSpec("flag_review", "Hold the draft for a person to review", cost="free", timeout_s=30), nodes.flag_node),
):
    REGISTRY.register(_spec, _tool(_node))
