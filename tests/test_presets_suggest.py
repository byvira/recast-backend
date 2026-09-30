"""Template structure suggestion: the answer is cleaned, never trusted. Pure (LLM stubbed)."""

import asyncio

from app.pipelines import presets_suggest as ps


def test_good_sections_are_kept_in_order():
    out = ps.clean_sections([
        {"section_name": "Hook", "char_limit": 120, "guidelines": "Open with a bold claim."},
        {"section_name": "Proof", "char_limit": 400, "guidelines": "Give one real example."},
    ])
    assert [s["section_name"] for s in out] == ["Hook", "Proof"]


def test_limits_are_clamped_and_bad_limits_get_a_default():
    out = ps.clean_sections([
        {"section_name": "A", "char_limit": 1, "guidelines": "x"},
        {"section_name": "B", "char_limit": 999999, "guidelines": "y"},
        {"section_name": "C", "char_limit": "lots", "guidelines": "z"},
    ])
    assert [s["char_limit"] for s in out] == [ps.MIN_CHARS, ps.MAX_CHARS, 280]


def test_repeats_blanks_and_non_objects_are_dropped():
    out = ps.clean_sections([
        "junk", {"section_name": "", "guidelines": "x"}, {"section_name": "Hook", "guidelines": ""},
        {"section_name": "Hook", "guidelines": "a"}, {"section_name": "hook", "guidelines": "b"},
        {"section_name": "Close", "guidelines": "c"},
    ])
    assert [s["section_name"] for s in out] == ["Hook", "Close"]


def test_one_section_is_not_a_structure_and_garbage_gives_nothing():
    assert ps.clean_sections([{"section_name": "Only", "guidelines": "x"}]) == []
    assert ps.clean_sections(None) == [] and ps.clean_sections("text") == []


def test_no_more_than_the_maximum():
    many = [{"section_name": f"S{i}", "guidelines": "g"} for i in range(20)]
    assert len(ps.clean_sections(many)) == ps.MAX_STEPS


def test_an_unusable_model_answer_gives_an_empty_suggestion(monkeypatch):
    async def fake(prompt, max_tokens=0):
        return None

    monkeypatch.setattr(ps, "call_llm_structured", fake)
    out = asyncio.run(ps.suggest_structure(title="T", category_label="Thread", description="", channels=[], existing_steps=[]))
    assert out == []


def test_the_prompt_carries_the_request_and_no_leak_bait(monkeypatch):
    seen = {}

    async def fake(prompt, max_tokens=0):
        seen["prompt"] = prompt
        return {"sections": [{"section_name": "Hook", "guidelines": "g"}, {"section_name": "Body", "guidelines": "g"}]}

    monkeypatch.setattr(ps, "call_llm_structured", fake)
    out = asyncio.run(ps.suggest_structure(
        title="Contrast framework", category_label="X thread", description="", channels=["x"],
        existing_steps=[{"section_name": "Mine", "guidelines": "keep me"}],
    ))
    assert len(out) == 2
    assert "Contrast framework" in seen["prompt"] and "Mine" in seen["prompt"] and "x" in seen["prompt"]
