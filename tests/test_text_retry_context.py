"""A rewrite is told what failed, shown the draft that failed, and asked for the same required phrases as the first attempt."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.agents.text import nodes
from app.models.text import Platform


def _state(**extra):
    base = {
        "emitter": None,
        "quality_issues": ["Generic opening: starts with 'In today's world'"],
        "extras": {
            "banned_words": [],
            "required_phrases": [{"text": "alpha"}, {"text": "beta"}, {"text": "gamma"}, {"text": "delta"}],
            "selected_phrases": [{"text": "alpha"}, {"text": "beta"}],
            "approved_openers": [],
            "approved_closers": [],
        },
        "tone_override_text": "",
        "goal_context": "",
        "content_brief": "",
        "normalised_content": "The source text.",
        "brand_context": "",
        "session_id": "s1",
        "current_platform": Platform.LINKEDIN,
        "retry_count": 0,
        "generated_content": "In today's world the first draft that failed.",
    }
    base.update(extra)
    return base


async def test_the_rewrite_prompt_contains_the_draft_that_failed(monkeypatch):
    captured = {}

    async def fake_generate(task):
        captured["task"] = task
        return SimpleNamespace(output={"content": "A better draft.", "quality_issues": []})

    monkeypatch.setattr(nodes, "generate_for_platform", fake_generate)
    out = await nodes.rewrite_node(_state())
    feedback = captured["task"].metadata["retry_feedback"]
    assert "the first draft that failed" in feedback
    assert "Generic opening" in feedback
    assert out["generated_content"] == "A better draft."


async def test_the_retry_asks_for_the_same_two_phrases_as_the_first_attempt(monkeypatch):
    captured = {}

    async def fake_generate(task):
        captured["task"] = task
        return SimpleNamespace(output={"content": "x", "quality_issues": []})

    monkeypatch.setattr(nodes, "generate_for_platform", fake_generate)
    out = await nodes.rewrite_node(_state())
    feedback = captured["task"].metadata["retry_feedback"]
    assert "alpha, beta" in feedback and "gamma" not in feedback
    assert [p["text"] for p in captured["task"].metadata["required_phrases"]] == ["alpha", "beta"]
    assert len(out["extras"]["required_phrases"]) == 4, "the full list stays in the state"
