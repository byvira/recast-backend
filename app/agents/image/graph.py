from langgraph.graph import END, StateGraph

from app.agents.base import BaseAgentState
from app.agents.image import nodes


def _after_plan(state: BaseAgentState) -> str:
    if state["status"] == "failed":
        return "finish"
    return "build_prompt" if (state["raw_input"] or {}).get("mode") == "topic" else "gate"


def _after_gate(state: BaseAgentState) -> str:
    io = state["intermediate_outputs"]
    if io.get("final_prompt"):
        return "generate"
    # one initial pass and one corrected pass, as before
    from app.pipelines.media import image_generation as ig

    return "settle" if int(io.get("gate_attempt", 0)) >= ig._MAX_POLISH_ATTEMPTS or io.get("verdict") == "error" else "gate"


def _after_settle(state: BaseAgentState) -> str:
    return "generate" if state["intermediate_outputs"].get("final_prompt") else "finish"


def _after_generate(state: BaseAgentState) -> str:
    io = state["intermediate_outputs"]
    if not io.get("image"):
        return "finish"
    return "qa" if (state["raw_input"] or {}).get("mode") == "topic" else "finish"


def build_image_agent():
    """Build and compile the image agent. The node name `plan_step` avoids a clash with the state's own `plan` key."""
    graph = StateGraph(BaseAgentState)
    graph.add_node("plan_step", nodes.plan_node)
    graph.add_node("build_prompt", nodes.build_prompt_node)
    graph.add_node("gate", nodes.gate_node)
    graph.add_node("settle", nodes.settle_node)
    graph.add_node("generate", nodes.generate_node)
    graph.add_node("qa", nodes.qa_node)
    graph.add_node("publish", nodes.publish_node)
    graph.add_node("finish", nodes.finish_node)

    graph.set_entry_point("plan_step")
    graph.add_conditional_edges("plan_step", _after_plan, {"build_prompt": "build_prompt", "gate": "gate", "finish": "finish"})
    graph.add_edge("build_prompt", "gate")
    graph.add_conditional_edges("gate", _after_gate, {"generate": "generate", "settle": "settle", "gate": "gate"})
    graph.add_conditional_edges("settle", _after_settle, {"generate": "generate", "finish": "finish"})
    graph.add_conditional_edges("generate", _after_generate, {"qa": "qa", "finish": "finish"})
    graph.add_edge("qa", "publish")
    graph.add_edge("publish", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


image_agent = build_image_agent()
