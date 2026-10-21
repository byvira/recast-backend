"""More small parsers around model replies: brief extraction, structured repurpose, image safety and the picture QA check. No model calls."""
from unittest.mock import AsyncMock

from app.models.text import AgentTask, Platform
from app.pipelines.media import image_generation as ig
from app.pipelines.text import normalizer, repurpose

_LONG = "A source article about planning a full week of posts for a small team. " * 3


async def test_brief_extraction_needs_real_content_and_survives_a_bad_reply(monkeypatch):
    stub = AsyncMock()
    monkeypatch.setattr(normalizer, "call_llm_structured", stub)
    assert await normalizer.extract_content_brief("", "en") == ""
    assert await normalizer.extract_content_brief("too short", "en") == ""
    stub.assert_not_called()

    stub.return_value = {}
    assert await normalizer.extract_content_brief(_LONG, "en") == ""
    stub.return_value = {"sharpest_angle": "  ", "concrete_detail": "", "avoid": "fluff"}
    assert await normalizer.extract_content_brief(_LONG, "en") == "", "no angle and no detail gives no brief"
    stub.side_effect = RuntimeError("down")
    assert await normalizer.extract_content_brief(_LONG, "en") == ""


async def test_brief_extraction_builds_a_brief_from_a_good_reply(monkeypatch):
    reply = {"sharpest_angle": "Planning beats posting", "concrete_detail": "Seven posts in one hour", "avoid": "hustle talk"}
    monkeypatch.setattr(normalizer, "call_llm_structured", AsyncMock(return_value=reply))
    brief = await normalizer.extract_content_brief(_LONG, "en")
    assert "Planning beats posting" in brief and "Seven posts in one hour" in brief


async def test_structured_repurpose_accepts_only_a_reply_with_a_list_of_sections(monkeypatch):
    task = AgentTask(
        agent="repurpose", platform=Platform.LINKEDIN, content="A source post.", brand_context="", session_id="s",
        metadata={"structure_rules": [{"section_name": "Hook", "char_limit": 100, "guidelines": "short"}]},
    )
    good = {"sections": [{"section_name": "Hook", "content": "Hi"}]}
    for reply, ok in ((good, True), ({}, False), ({"sections": "nope"}, False), ({"other": []}, False)):
        monkeypatch.setattr(repurpose, "call_llm_structured", AsyncMock(return_value=reply))
        result = await repurpose.run_structured_repurpose_agent(task, Platform.TWITTER)
        assert result.success is ok
        assert result.output == (reply if ok else {})


async def test_image_safety_reads_the_reply_and_retries_once_before_failing_closed(monkeypatch):
    async def no_wait(_):
        return None

    monkeypatch.setattr(ig.asyncio, "sleep", no_wait)
    for reply, verdict in (("YES", "unsafe"), ("yes, because", "unsafe"), ("NO", "safe"), ("no", "safe")):
        monkeypatch.setattr(ig, "call_llm", AsyncMock(return_value=reply))
        assert await ig._safety_verdict("a calm lake") == verdict
    monkeypatch.setattr(ig, "call_llm", AsyncMock(side_effect=[RuntimeError("busy"), "NO"]))
    assert await ig._safety_verdict("a calm lake") == "safe", "the second try answers"
    stub = AsyncMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr(ig, "call_llm", stub)
    assert await ig._safety_verdict("a calm lake") == "error"
    assert stub.await_count == 2
    assert await ig._safety_gate("a calm lake") is False, "a check that cannot run stops the picture"


async def test_picture_qa_flags_only_a_clear_no_and_never_on_a_failed_check(monkeypatch):
    brand = {"visual_identity": {"visual_style_notes": "Warm, hand drawn"}}
    cases = (
        ("YES", (False, None)),
        ("NO: too dark", (True, "too dark")),
        ("NO", (True, "Doesn't match the brand's visual style.")),
        ("", (False, None)),
    )
    for reply, expected in cases:
        monkeypatch.setattr(ig, "call_vision", AsyncMock(return_value=reply))
        assert await ig._qa_gate(b"img", brand) == expected
    monkeypatch.setattr(ig, "call_vision", AsyncMock(side_effect=RuntimeError("down")))
    assert await ig._qa_gate(b"img", brand) == (False, None)
    stub = AsyncMock(return_value="NO")
    monkeypatch.setattr(ig, "call_vision", stub)
    assert await ig._qa_gate(b"img", {}) == (False, None), "nothing to check against"
    stub.assert_not_called()
