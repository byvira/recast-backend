"""Required phrases accept a faithful paraphrase, vague-word feedback never asks for an invented number, and length limits are advisory."""
from app.models.text import Platform
from app.pipelines.text.generator import validate_content


def _validate(content, platform=Platform.LINKEDIN, phrases=None):
    return validate_content(content, platform, [], phrases or [], [], [], "en")


def test_a_required_phrase_in_other_words_still_counts():
    ok, issues = _validate(
        "We keep every post true to your voice, so each post stays true to you.",
        phrases=[{"text": "True to your voice"}],
    )
    assert not any("Required brand phrase" in i for i in issues)


def test_a_required_phrase_that_is_absent_is_still_flagged():
    ok, issues = _validate("A post about something else entirely.", phrases=[{"text": "True to your voice"}])
    assert any("Required brand phrase missing" in i for i in issues)


def test_weasel_word_feedback_says_never_to_invent_a_number():
    ok, issues = _validate("Many teams struggle with this.")
    weasel = [i for i in issues if i.startswith("Weasel words found")]
    assert weasel and "never invent a number" in weasel[0]


def test_a_long_or_over_hashtagged_post_is_advisory_not_a_failure():
    text = ("A plain sentence about the plan for the week ahead. " * 40) + "#one #two #three #four"
    ok, issues = _validate(text)
    assert any(i.startswith("Advisory: Longer than the usual 300 words") for i in issues)
    assert any(i.startswith("Advisory: 4 hashtags") for i in issues)
    assert not any("Longer than" in i or "hashtags" in i for i in issues if not i.startswith("Advisory"))


async def test_a_long_source_keeps_its_tail_after_cleaning(monkeypatch):
    from unittest.mock import AsyncMock

    from app.pipelines.text import normalizer

    head_text = ("Paragraph one of the source. " * 30 + "\n") * 10
    tail_text = "THE VERY LAST PARAGRAPH WITH A KEY FACT."
    source = head_text + tail_text
    assert len(source) > 6000
    monkeypatch.setattr(normalizer, "call_llm_structured", AsyncMock(side_effect=lambda prompt, **k: {"cleaned": "cleaned head " * 200}))
    out = await normalizer.clean_raw_content(source)
    assert out.endswith(tail_text)


def test_a_quick_edit_that_runs_past_the_platform_is_noted_in_plain_words():
    from app.pipelines.text.generator import limit_warnings

    assert limit_warnings("Twitter/X", "x" * 300) and "280" in limit_warnings("Twitter/X", "x" * 300)[0]
    assert limit_warnings("LinkedIn", "word " * 400)
    assert limit_warnings("LinkedIn", "a short fine post #one") == []
    assert limit_warnings("NotAPlatform", "anything") == []
