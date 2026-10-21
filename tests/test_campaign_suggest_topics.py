"""Campaign topic suggestions: the reply is trimmed, an unusable reply gives an empty suggestion, and the call is counted for the workspace. No model calls."""
from unittest.mock import AsyncMock

from app.pipelines.campaigns import suggest


async def test_suggestions_keep_only_real_text_and_at_most_three(monkeypatch):
    reply = {"suggested_topics": ["First", "", "  ", 7, "Second", "Third", "Fourth"], "suggested_tone": "warm", "rationale": "why"}
    monkeypatch.setattr(suggest, "call_llm_structured", AsyncMock(return_value=reply))
    out = await suggest.suggest_campaign_topics("a brief", "brand", [])
    assert out["suggested_topics"] == ["First", "Second", "Third"]
    assert out["suggested_tone"] == "warm"


async def test_an_unusable_reply_gives_an_empty_suggestion(monkeypatch):
    for bad in ({}, {"suggested_topics": []}, {"suggested_topics": None}):
        monkeypatch.setattr(suggest, "call_llm_structured", AsyncMock(return_value=bad))
        out = await suggest.suggest_campaign_topics("a brief", "brand", ["old"])
        assert out == {"suggested_topics": [], "suggested_tone": None, "rationale": ""}


async def test_the_call_is_counted_for_the_workspace(monkeypatch):
    seen: list = []
    monkeypatch.setattr(suggest, "set_usage_workspace", lambda ws: seen.append(ws))
    monkeypatch.setattr(suggest, "call_llm_structured", AsyncMock(return_value={"suggested_topics": ["A"]}))
    await suggest.suggest_campaign_topics("a brief", "brand", [], workspace_id="ws-1")
    assert seen == ["ws-1"]
