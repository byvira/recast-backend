"""The audio director's optional steps and the text agent's place on the shared runtime. No signup, no provider call: the plan, the
registry, the decision log and the director's own tool wrappers, with the route handlers behind them replaced."""
from uuid import uuid4

import pytest

from app.agents.audio import director
from app.agents.text import nodes as text_nodes
from app.agents.text import steps as text_steps
from app.db.mongo import get_db

NARRATION = {"title": "Episode", "brand_id": "b1", "script": "A short script to voice."}


def _tools(settings: director.DirectorSettings) -> list[str]:
    return [s.tool for s in director.build_plan(settings)]


def test_an_episode_with_every_extra_plans_them_in_a_sensible_order():
    plan = _tools(director.DirectorSettings(
        goal="make_episode", narration=dict(NARRATION), fit_seconds=90, localize=["Tamil", "French"], clip_suggestions=True, export_format="wav",
    ))
    assert plan[0] == "fit_script" and plan[1] == "narrate"
    assert plan.index("sound_checks") < plan.index("localize") < plan.index("clip_suggestions") < plan.index("export")
    assert plan.count("localize") == 2


def test_extras_are_left_out_when_they_are_not_asked_for():
    plan = _tools(director.DirectorSettings(goal="make_episode", narration=dict(NARRATION)))
    assert not {"fit_script", "localize", "clip_suggestions", "export"} & set(plan)
    plain = _tools(director.DirectorSettings(goal="clean_and_check", localize=["Tamil"], export_format="mp3"))
    assert not {"localize", "export"} & set(plain)          # these goals never make versions or exports


def test_fitting_is_only_planned_when_there_is_a_script_to_fit():
    assert "fit_script" not in _tools(director.DirectorSettings(goal="make_episode", narration={"title": "t", "brand_id": "b"}, fit_seconds=60))


def test_every_planned_tool_is_registered():
    names = set(director.REGISTRY.names())
    for goal in ("make_episode", "publish_ready", "show_notes", "clean_and_check"):
        plan = _tools(director.DirectorSettings(
            goal=goal, narration=dict(NARRATION), cleanup={"x": 1}, assemble={"y": 1}, fit_seconds=60, localize=["Tamil"],
            clip_suggestions=True, export_format="mp3",
        ))
        assert set(plan) <= names


async def test_a_fitted_script_replaces_the_one_that_will_be_voiced(monkeypatch):
    seen = {}

    async def fake_fit(**kwargs):
        seen["target"] = kwargs["body"].target_seconds
        return {"script": "A script of the right length.", "status": "fitted", "estimated_seconds": 88}

    monkeypatch.setattr(director, "_inner", lambda name: fake_fit)
    monkeypatch.setattr("app.core.rbac.assert_permission", lambda *a, **k: None)
    state = {"ctx": type("C", (), {"member": object()})()}
    request = dict(NARRATION)
    out = await director._tool_fit_script(state=state, request=request, target_seconds=90)
    assert seen["target"] == 90 and out["status"] == "fitted"
    assert request["script"] == "A script of the right length."


async def test_a_script_that_cannot_be_fitted_is_kept_as_it_was(monkeypatch):
    async def fake_fit(**kwargs):
        return {"script": "something else", "status": "unchanged", "estimated_seconds": 30}

    monkeypatch.setattr(director, "_inner", lambda name: fake_fit)
    monkeypatch.setattr("app.core.rbac.assert_permission", lambda *a, **k: None)
    request = dict(NARRATION)
    await director._tool_fit_script(state={"ctx": type("C", (), {"member": object()})()}, request=request, target_seconds=90)
    assert request["script"] == NARRATION["script"]


def test_the_text_steps_are_on_the_shared_registry_with_their_costs():
    names = text_steps.REGISTRY.names()
    assert names == ["normalise", "build_context", "generate", "generate_hooks", "generate_seo", "quality_check", "rewrite", "flag_review"]
    assert text_steps.REGISTRY.spec("generate").cost == "llm"
    assert text_steps.REGISTRY.spec("normalise").cost == "free"


async def test_a_post_held_for_review_leaves_a_decision_the_other_agents_can_read():
    workspace_id = f"ws-{uuid4()}"
    state = {
        "current_platform": "linkedin", "session_id": "s1", "quality_issues": ["The post is too long.", "Advisory: shorter is better."],
        "emitter": None, "workspace_id": workspace_id, "user_id": "u1",
    }
    out = await text_nodes.flag_node(state)
    assert out == {"flagged_for_review": True}
    entry = await get_db()["agent_decisions"].find_one({"workspace_id": workspace_id, "agent": "text"})
    try:
        assert entry and entry["step"] == "flag" and entry["outcome"] == "needs_decision"
        assert entry["evidence"]["issues"] == ["The post is too long."] and entry["evidence"]["platform"] == "linkedin"
    finally:
        await get_db()["agent_decisions"].delete_many({"workspace_id": workspace_id})
