"""The language chain and the model fallback chain of the safety check: what language a text is, who reads what the rules cannot, what
happens when a model fails, the silent second opinion, and a hold reason in the member's language. No real model is called."""
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.content_guard import agent as guard_agent
from app.agents.content_guard import config as guard_config
from app.agents.content_guard import judges
from app.agents.content_guard.agent import guard_piece_doc, hold_message, review_text
from app.agents.content_guard.rules import ScreenResult
from app.core.config import settings
from app.db.mongo import content_safety, safety_events
from app.shared.language_profile import profile, script_of


@pytest.fixture(autouse=True)
async def clean_state(monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUARD_LIVE_CHECKS", True)
    await content_safety.delete_many({})
    await safety_events.delete_many({})
    await judges.log_col().delete_many({})
    guard_config.reset_cache()
    guard_agent._model_verdicts.clear()
    guard_agent._contact_cache.clear()
    judges.reset()
    yield
    await content_safety.delete_many({})
    await safety_events.delete_many({})
    await judges.log_col().delete_many({})
    judges.reset()


# ---- the language chain ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, asked, code, source, can_read", [
    ("Our new service starts today", None, "en", "detected", True),
    ("यह हमारी नई सेवा है, आज ही जुड़ें", None, "hi", "script", False),
    ("எங்கள் புதிய சேவை இன்று தொடங்குகிறது", None, "ta", "script", False),
    ("మా కొత్త సేవ ఈ రోజు మొదలవుతుంది", None, "te", "script", False),
    ("আমাদের নতুন সেবা আজ শুরু হচ্ছে", None, "bn", "script", False),
    ("Naalaiku offer irukku, vaanga", "ta+en", "ta+en", "requested", False),
    ("Aaj ka offer miss mat karo", "hi+en", "hi+en", "requested", False),
    ("Our new service starts today", "en", "en", "requested", True),
    ("Hola amigos, hoy tenemos una oferta especial para todos ustedes en la tienda", None, "es", "detected", False),
])
def test_the_chain_places_a_text_and_says_whether_the_rules_can_read_it(text, asked, code, source, can_read):
    p = profile(text, asked)
    assert (p.code, p.source, p.rules_can_read) == (code, source, can_read)
    assert p.regional is (not can_read)


def test_a_language_nobody_listed_still_goes_through_the_chain():
    p = profile("Anything at all", "xx-unknown")
    assert p.code == "xx-unknown" and p.regional and p.source == "requested"
    assert profile("", None).code == "unknown"


def test_asking_for_english_but_writing_in_another_script_is_not_trusted():
    assert profile("यह हमारी नई सेवा है", "en").regional


def test_scripts_are_counted_by_letters_not_by_digits_or_marks():
    assert script_of("Tamil எண் 123 and English")[0] == "latin"
    assert script_of("எங்கள் புதிய சேவை 123")[0] == "tamil"


# ---- who reads what the rules cannot -----------------------------------------------------------------------------------------------
async def test_a_tanglish_post_reaches_the_model_when_it_was_asked_for(monkeypatch):
    await guard_config.save({"model_check": "risky"}, expected_version=0, user_id="u")
    seen = []

    async def read(text):
        seen.append(text)
        return ScreenResult()

    monkeypatch.setattr(guard_agent, "_model_screen", read)
    await review_text("Naalaiku offer irukku, vaanga", where="generation", language="ta+en")
    assert seen
    seen.clear()
    await review_text("Tomorrow there is an offer, come along", where="generation", language="en")
    assert not seen, "plain English with no risk signal stays on the free rules"


async def test_the_piece_language_is_used_when_the_post_is_saved():
    doc = {"content": "यह हमारी नई सेवा है", "language": "hi", "workspace_id": "w1", "piece_id": "p1", "platform": "LinkedIn"}
    with patch.object(guard_agent, "_model_screen", AsyncMock(return_value=ScreenResult())) as model:
        await guard_config.save({"model_check": "risky"}, expected_version=0, user_id="u")
        await guard_piece_doc(doc, where="generation")
    assert model.called


# ---- the model fallback chain ---------------------------------------------------------------------------------------------------
def test_the_chain_follows_ops_and_always_ends_with_the_everyday_model():
    assert judges.chain({"judge_models": ["safeguard", "fast"]}) == ["safeguard", "fast"]
    assert judges.chain({"judge_models": ["safeguard"]}) == ["safeguard", "fast"]
    assert judges.chain({"judge_models": ["nonsense", "balanced"]}) == ["balanced", "fast"]
    assert judges.chain({}) == ["fast"]


async def test_when_the_first_judge_fails_the_next_one_answers(monkeypatch):
    await guard_config.save({"judge_models": ["safeguard", "fast"], "model_check": "publish"}, expected_version=0, user_id="u")
    asked = []

    async def read(judge, text):
        asked.append(judge)
        if judge == "safeguard":
            raise RuntimeError("model not available")
        return ScreenResult()

    monkeypatch.setattr(guard_agent, "_read_with", read)
    verdict = await guard_agent._model_screen("A calm post about the roadmap.")
    assert verdict.ok and asked == ["safeguard", "fast"]


async def test_a_judge_that_keeps_failing_is_skipped_for_a_while(monkeypatch):
    await guard_config.save({"judge_models": ["safeguard", "fast"]}, expected_version=0, user_id="u")
    asked = []

    async def read(judge, text):
        asked.append(judge)
        if judge == "safeguard":
            raise RuntimeError("down")
        return ScreenResult()

    monkeypatch.setattr(guard_agent, "_read_with", read)
    for i in range(judges.MAX_FAILURES):
        await guard_agent._model_screen(f"post number {i}")
    asked.clear()
    await guard_agent._model_screen("one more post")
    assert asked == ["fast"], "the failing judge is not asked again until the wait is over"
    judges.note_ok("safeguard")
    assert judges.usable("safeguard")


async def test_when_every_judge_fails_the_read_raises_and_is_not_remembered(monkeypatch):
    async def read(judge, text):
        raise RuntimeError("down")

    monkeypatch.setattr(guard_agent, "_read_with", read)
    with pytest.raises(RuntimeError):
        await guard_agent._model_screen("anything")
    assert not guard_agent._model_verdicts


# ---- the silent second opinion ---------------------------------------------------------------------------------------------------
async def test_the_silent_judge_is_compared_and_never_obeyed(monkeypatch):
    await guard_config.save({"judge_models": ["fast"], "shadow_model": "safeguard", "shadow_rate": 1.0}, expected_version=0, user_id="u")

    async def read(judge, text):
        return ScreenResult() if judge == "fast" else ScreenResult(ok=False, categories=["claims"], matches=["x"])

    monkeypatch.setattr(guard_agent, "_read_with", read)
    verdict = await guard_agent._model_screen("Call 98765 43210 for the offer of the week")
    assert verdict.ok, "the answering judge decides; the silent one is only compared"
    await __import__("asyncio").gather(*list(judges._tasks))
    summary = await judges.comparison_summary(30)
    assert summary["compared"] == 1 and summary["agreement"] == 0.0 and len(summary["disagreements"]) == 1
    assert "98765" not in summary["disagreements"][0]["excerpt"], "details are hidden in the log"


async def test_no_silent_comparison_when_it_is_off(monkeypatch):
    async def read(judge, text):
        return ScreenResult()

    monkeypatch.setattr(guard_agent, "_read_with", read)
    await guard_agent._model_screen("A calm post about the roadmap.")
    assert judges.should_shadow({"shadow_model": "off", "shadow_rate": 1.0}) is None
    assert judges.should_shadow({"shadow_model": "safeguard", "shadow_rate": 0}) is None
    assert await judges.log_col().count_documents({}) == 0


async def test_ops_can_save_the_chain_and_bad_names_are_dropped():
    saved = await guard_config.save({"judge_models": ["safeguard", "bogus", "fast", "safeguard"], "shadow_model": "bogus", "shadow_rate": 5}, expected_version=0, user_id="u")
    assert saved["judge_models"] == ["safeguard", "fast"] and saved["shadow_model"] == "off" and saved["shadow_rate"] == 1.0


# ---- a hold reason in the member's language ------------------------------------------------------------------------------------
async def test_the_hold_reason_is_in_english_for_english_and_for_a_blend():
    english = await hold_message(["claims"], "en")
    assert "claims that cannot be backed up" in english
    assert await hold_message(["claims"], "ta+en") == english
    assert await hold_message(["claims"], None) == english


async def test_the_hold_reason_is_translated_for_another_language_and_falls_back_to_english():
    async def fake(key, language, template, ctx=None):
        return f"[{language}] " + template.format(**(ctx or {}))

    with patch("app.shared.localized_strings.get_localized_string", new=fake):
        text = await hold_message(["claims", "personal_data"], "hi")
    assert text.startswith("[hi] ") and "[hi] claims that cannot be backed up" in text

    async def broken(*a, **k):
        raise RuntimeError("no translation")

    with patch("app.shared.localized_strings.get_localized_string", new=broken):
        assert (await hold_message(["claims"], "hi")).startswith("This post was held back")


async def test_a_check_that_ran_on_the_rules_alone_because_no_judge_answered_is_counted_for_ops(monkeypatch):
    await judges.misses_col().delete_many({})
    await guard_config.save({"model_check": "risky"}, expected_version=0, user_id="u")

    async def down(text):
        raise RuntimeError("every judge is down")

    monkeypatch.setattr(guard_agent, "_model_screen", down)
    result = await review_text("Naalaiku offer irukku, vaanga", where="generation", language="ta+en")
    assert result.outcome in ("clean", "fixed"), "the post is still judged by the rules, as before"
    await review_text("Naalaiku offer irukku, vaanga", where="generation", language="ta+en")

    summary = await judges.comparison_summary(30)
    assert summary["model_unavailable"]["total"] == 2
    assert summary["model_unavailable"]["by_day"][0]["count"] == 2
    await judges.misses_col().delete_many({})
