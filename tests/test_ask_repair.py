"""The planning model's small slips are put right, and what it is told about each input names the allowed values."""
from app.ask import pipeline, repair
from app.tools.registry import get_registry

ARGS = {"source_type": "topic", "content": "a post about my comeback", "platforms": "facebook", "brand_id": "default"}


def _model():
    return get_registry().get("text.generate").input_model


def test_one_platform_written_as_plain_text_becomes_a_list_in_the_tool_spelling():
    fixed = repair.repair_arguments(_model(), ARGS)
    assert [str(p.value if hasattr(p, "value") else p) for p in fixed["platforms"]] == ["Facebook"]


def test_several_platforms_in_one_string_become_a_list():
    fixed = repair.repair_arguments(_model(), {**ARGS, "platforms": "facebook, linkedin and instagram"})
    assert [str(p) for p in fixed["platforms"]] == ["Facebook", "LinkedIn", "Instagram"]


def test_a_short_name_means_the_shortest_platform_that_begins_with_it():
    assert repair.repair_arguments(_model(), {**ARGS, "platforms": ["twitter"]})["platforms"] == ["Twitter/X"]


def test_a_value_that_cannot_be_read_with_confidence_is_left_alone_so_the_check_still_refuses_it():
    fixed = repair.repair_arguments(_model(), {**ARGS, "source_type": "user_input"})
    assert fixed["source_type"] == "user_input"
    from pydantic import ValidationError
    try:
        _model().model_validate(fixed)
        raise AssertionError("it should not be valid")
    except ValidationError as exc:
        assert any(e["loc"] == ("source_type",) for e in exc.errors())


def test_repaired_arguments_pass_the_tools_own_check():
    _model().model_validate(repair.repair_arguments(_model(), ARGS))


def test_a_value_taken_from_an_earlier_step_is_never_touched():
    args = {**ARGS, "platforms": "${pick.platform}"}
    assert repair.repair_arguments(_model(), args)["platforms"] == "${pick.platform}"


async def test_a_placeholder_brand_becomes_the_workspaces_main_brand(monkeypatch):
    async def main_brand(workspace_id):
        return "brand-main"

    monkeypatch.setattr(repair, "default_brand_id", main_brand)

    class Step:
        tool = "text.generate"
        arguments = dict(ARGS)

    step = Step()
    await repair.repair_steps([step], get_registry(), "w1")
    assert step.arguments["brand_id"] == "brand-main"
    step.arguments = {**ARGS, "brand_id": "a-real-id"}
    await repair.repair_steps([step], get_registry(), "w1")
    assert step.arguments["brand_id"] == "a-real-id"


def test_the_planner_is_told_the_allowed_values_and_which_inputs_are_lists():
    line = pipeline._inputs(get_registry().get("text.generate"))
    assert "source_type*: text|topic|url|transcript" in line
    assert "platforms*: a list of Blog|Newsletter" in line and "Facebook" in line
    assert "brand_id*" in line


def test_the_brief_says_how_to_name_the_main_brand_and_to_use_the_values_as_shown():
    _, system = pipeline.plan_prompt("write a post", [get_registry().get("text.generate")])
    assert '"default"' in system and "spelled as shown" in system


def test_asking_for_ideas_or_tips_is_a_reply_and_never_none():
    _, system = pipeline.decide_prompt("give me post ideas for facebook", [get_registry().get("text.generate")], "en", allow_reply=True)
    assert "brainstorm" in system and "ideas" in system
    assert 'are a "reply", never "none"' in system
    assert 'always a "tool" request' in system and "never write the post yourself" in system        # writing something is the tool's job, not a reply


# ── which model fills in the details ─────────────────────────────────────────

def _charge(log):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def charge(step):
        log.append(step)
        yield

    return charge


async def test_a_request_one_tool_does_is_filled_in_by_the_small_model_and_a_real_plan_by_the_larger_one():
    from app.ask import router
    from app.shared.llm import GroqModel

    assert router.DEFAULTS["fill"].model == GroqModel.FAST.value and router.DEFAULTS["plan"].model == GroqModel.BALANCED.value
    spec = get_registry().get("text.generate")
    roles: list[str] = []

    async def think(role, prompt, system):
        roles.append(role)
        if role == "decide":
            return {"action": "tool", "tool": spec.name}
        return {"steps": [{"label": "write", "tool": spec.name, "arguments": ARGS}]}

    got = await pipeline.understand("write a post for facebook", [spec], "en", think=think, charge=_charge([]), shortlisted=[spec.name], how="keywords")
    assert got.action == "tool" and roles == ["decide", "fill"]            # the larger model is not asked at all


async def test_a_request_with_several_steps_still_uses_the_larger_model():
    specs = [get_registry().get("text.generate"), get_registry().get("library.create_draft")]
    roles: list[str] = []

    async def think(role, prompt, system):
        roles.append(role)
        if role == "decide":
            return {"action": "plan"}
        return {"steps": [{"label": "one", "tool": specs[0].name, "arguments": ARGS}, {"label": "two", "tool": specs[1].name, "arguments": {}}]}

    await pipeline.understand("write it and save it", specs, "en", think=think, charge=_charge([]), shortlisted=[s.name for s in specs], how="keywords")
    assert roles == ["decide", "plan"]


def test_staff_can_change_the_new_job_like_the_others():
    from app.ask import router

    assert "fill" in router.DEFAULTS and router.DEFAULTS["fill"].private


def test_a_what_can_you_do_question_is_answered_in_everyday_words_not_tool_words():
    _, system = pipeline.decide_prompt("what can you do for me?", [get_registry().get("text.generate")], "en", allow_reply=True)
    assert "When asked what you can do" in system and "never name a tool" in system
    assert pipeline.decide_prompt("hi", [get_registry().get("text.generate")], "en", allow_reply=False)[1].count("never name a tool") == 0


def test_the_nothing_fits_message_is_plain_and_has_no_tool_words():
    from app.ask import runs

    text = runs.MESSAGES["none"].lower()
    assert "tool" not in text and "post" in text
