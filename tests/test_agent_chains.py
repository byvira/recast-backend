"""The order each pipeline agent runs its steps in, checked by running the real compiled graphs.

Only the language-model calls are replaced (no provider quota); the graphs, the routing, the retry rule and the state are real.
These are the sequence guarantees: planning first, a bounded retry loop that stops at the limit, and a delivery at the end."""
import importlib

import pytest

from app.agents.base import initial_state


def _structured_factory(state: dict):
    """Answers each structured call by what the prompt asks for: a plan, an analysis, or a score."""
    async def fake(*args, prompt: str = "", **kwargs):
        prompt = prompt or (args[0] if args else "")
        if "Evaluate" in prompt:
            state["evaluations"] += 1
            score = state["scores"][min(state["evaluations"] - 1, len(state["scores"]) - 1)]
            # every dimension any pipeline scores (audio: completeness and accuracy; video: platform fit and engagement)
            return {"completeness": score, "brand_alignment": score, "accuracy": score, "platform_fit": score,
                    "engagement_potential": score, "overall": score}
        if "plan" in prompt.lower() and "steps" in prompt:
            state["plans"] += 1
            return {"steps": ["transcribe", "analyse", "generate", "evaluate", "deliver"]}
        return {"topics": ["focus"], "key_quotes": ["one quote"]}
    return fake


@pytest.fixture(params=["audio", "video"])
def agent(request, monkeypatch):
    nodes = importlib.import_module(f"app.agents.{request.param}.nodes")
    graph = importlib.import_module(f"app.agents.{request.param}.graph")
    state = {"evaluations": 0, "plans": 0, "scores": [0.9], "drafts": 0}

    async def fake_plain(*args, **kwargs):
        state["drafts"] += 1
        return f"draft number {state['drafts']}"

    monkeypatch.setattr(nodes, "call_llm_structured", _structured_factory(state))
    monkeypatch.setattr(nodes, "call_llm", fake_plain)
    compiled = getattr(graph, f"{request.param}_agent")
    state["nodes"] = nodes
    return compiled, state


def _run_input():
    return {"title": "Weekly show", "transcript": "We talked about focus and habits for a long while today.", "output_type": "show_notes"}


async def test_a_good_first_draft_is_delivered_without_a_retry(agent):
    compiled, state = agent
    final = await compiled.ainvoke(initial_state(user_id="u", brand={"name": "Acme"}, raw_input=_run_input()))
    assert final["status"] == "completed" and final["retry_count"] == 0
    assert state["plans"] == 1 and state["drafts"] == 1 and state["evaluations"] == 1
    assert final["outputs"]["content"] == "draft number 1"
    assert final["plan"][0] == "transcribe"


async def test_a_weak_draft_is_rewritten_once_and_the_better_one_is_delivered(agent):
    compiled, state = agent
    state["scores"] = [0.3, 0.9]
    final = await compiled.ainvoke(initial_state(user_id="u", brand={"name": "Acme"}, raw_input=_run_input()))
    assert final["retry_count"] == 1 and state["drafts"] == 2 and state["evaluations"] == 2
    assert final["outputs"]["content"] == "draft number 2"
    assert [c.get("retry") for c in final["tool_calls"] if c["node"] == "generate"] == [0, 1]
    assert any("Retrying" in e for e in final["errors"])


async def test_a_draft_that_never_improves_stops_at_the_limit_and_still_delivers(agent):
    compiled, state = agent
    state["scores"] = [0.2]
    final = await compiled.ainvoke(initial_state(user_id="u", brand={"name": "Acme"}, raw_input=_run_input(), max_retries=2))
    assert final["retry_count"] == 2 and state["drafts"] == 3 and final["status"] == "completed"


async def test_a_plan_the_model_cannot_give_falls_back_to_the_standard_steps(agent, monkeypatch):
    compiled, state = agent

    async def empty(*args, **kwargs):
        return {}
    monkeypatch.setattr(state["nodes"], "call_llm_structured", empty)
    final = await compiled.ainvoke(initial_state(user_id="u", brand={"name": "Acme"}, raw_input=_run_input()))
    assert final["plan"] == ["transcribe", "analyse", "generate", "evaluate", "deliver"]


# ── the text agent's chain ───────────────────────────────────────────────────────────────────────────────────────────────

def test_the_text_agent_runs_every_step_in_order_and_ends_at_output():
    from app.agents.text.graph import build_single_platform_graph

    graph = build_single_platform_graph().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    chain = ["normalise", "build_context", "generate", "generate_hooks", "generate_seo", "quality_check"]
    for a, b in zip(chain, chain[1:]):
        assert (a, b) in edges, f"{a} should lead to {b}"
    assert ("__start__", "normalise") in edges and ("collect_output", "__end__") in edges
    # the quality check either finishes, rewrites (back through hooks and SEO), or flags the post for review
    assert {("quality_check", "collect_output"), ("quality_check", "rewrite"), ("quality_check", "flag_review")} <= edges
    assert ("rewrite", "generate_hooks") in edges and ("flag_review", "collect_output") in edges


def test_the_image_agent_runs_in_order_and_every_branch_ends():
    from app.agents.image.graph import image_agent

    edges = {(e.source, e.target) for e in image_agent.get_graph().edges}
    assert ("__start__", "plan_step") in edges and ("finish", "__end__") in edges
    assert {("build_prompt", "gate"), ("qa", "publish"), ("publish", "finish")} <= edges
    # the gate may loop once on itself; a failed gate settles; nothing is left without a way to finish
    assert {("gate", "gate"), ("gate", "settle"), ("gate", "generate"), ("settle", "finish"), ("generate", "finish")} <= edges
