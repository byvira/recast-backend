"""Claims and personal details: what the free rules find, what they leave alone, the brand's own contacts, other scripts' digits, and
that staff never read the details themselves. No model is called."""
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.content_guard import agent as guard_agent
from app.agents.content_guard import config as guard_config
from app.agents.content_guard.agent import review_text
from app.agents.content_guard.rules import risk_signals, screen_text
from app.agents.content_guard.sensitive import ascii_digits, contact_keys, luhn_ok, mask_personal, verhoeff_ok
from app.core.config import settings
from app.db.mongo import content_safety, safety_events

AADHAAR = "812806192057"  # passes the Aadhaar check digit; not a real person's number
CARD = "4111 1111 1111 1111"  # the standard test card number


@pytest.fixture(autouse=True)
async def clean_state(monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUARD_LIVE_CHECKS", True)
    await content_safety.delete_many({})
    await safety_events.delete_many({})
    guard_config.reset_cache()
    guard_agent._model_verdicts.clear()
    guard_agent._contact_cache.clear()
    yield
    await content_safety.delete_many({})
    await safety_events.delete_many({})


# ---- claims -----------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Guaranteed returns of 40% every month. Join today.",
    "This tea cures diabetes in two weeks.",
    "A 100% safe and risk-free investment.",
    "Lose 10 kg in 7 days with this one pill.",
    "The miracle cure doctors hide from you.",
    "Earn Rs 5,000 per day from home, guaranteed.",
])
def test_claims_a_brand_cannot_back_up_are_found(text):
    result = screen_text(text)
    assert not result.ok and "claims" in result.categories


@pytest.mark.parametrize("text", [
    "Past results are not a guarantee, and returns are never guaranteed returns.",
    "We cannot promise results, but we will walk you through every step.",
    "Our yoga classes may help you relax. Talk to your doctor about diabetes care.",
    "Join the guided course: a plan, a coach and weekly check-ins.",
    "Book a free demo and see the dashboard.",
])
def test_honest_wording_is_not_a_claim(text):
    assert screen_text(text).ok


# ---- personal and financial details -------------------------------------------------------------------------------------------
def test_the_check_digits_tell_real_formats_from_random_numbers():
    assert verhoeff_ok(AADHAAR) and not verhoeff_ok("812806192058")
    assert luhn_ok(CARD.replace(" ", "")) and not luhn_ok("4111111111111112")


@pytest.mark.parametrize("text, shown", [
    (f"My Aadhaar is {AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]}", "Aadhaar number"),
    (f"Pay with card {CARD}", "card number"),
    ("My PAN is ABCDE1234F", "PAN"),
    ("Send it to account IFSC SBIN0001234", "bank code (IFSC)"),
    ("Call Ravi on 98765 43210 tonight", "phone number"),
    ("Mail ravi.kumar@example.com for details", "email address"),
])
def test_personal_details_are_found_and_named(text, shown):
    result = screen_text(text)
    assert not result.ok and result.categories == ["personal_data"] and shown in result.matches


def test_a_random_twelve_digit_number_is_not_an_aadhaar_number():
    assert screen_text("Order number 812806192058 has shipped.").ok


def test_digits_in_other_scripts_are_read_as_digits():
    devanagari = ascii_digits("आधार ८१२८ ०६१९ २०५७")
    assert "8128 0619 2057" in devanagari
    assert not screen_text("आधार ८१२८ ०६१९ २०५७ है").ok
    tamil = "எண் ௯௮௭௬௫ ௪௩௨௧௦"
    assert not screen_text(tamil).ok, "a phone number written in Tamil numerals is still a phone number"


def test_a_brand_may_publish_its_own_contacts_but_not_someone_elses():
    own = contact_keys("Call us on +91 98765 43210 or write to hello@shop.in. Visit www.shop.in")
    assert screen_text("Call us on 98765 43210", own_contacts=own).ok
    assert screen_text("Write to hello@shop.in or sales@shop.in", own_contacts=own).ok, "the brand's own domain is allowed"
    other = screen_text("Call my cousin on 91234 56789", own_contacts=own)
    assert not other.ok and "personal_data" in other.categories
    assert not screen_text("Mail someone@gmail.com", own_contacts=own).ok
    assert screen_text("Free helpline 1800 123 4567 for every customer").ok


def test_a_toll_free_line_and_a_gstin_are_not_personal_details():
    assert screen_text("GSTIN 29ABCDE1234F1Z5 and helpline 1800 103 1234").ok


def test_staff_never_read_the_details_themselves():
    masked = mask_personal(f"PAN ABCDE1234F, card {CARD}, call 9876543210, mail a@b.com")
    for secret in ("ABCDE1234F", "4111", "9876543210", "a@b.com"):
        assert secret not in masked
    assert masked.count("[hidden]") == 4


async def test_the_event_list_keeps_the_details_hidden():
    with patch("app.shared.llm.call_llm", new=AsyncMock(return_value="Call our team via the website.")):
        result = await review_text("Pay to ABCDE1234F or call 98765 43210", where="generation", workspace_id="w1", own_contacts=[])
    assert result.outcome in ("rewritten", "blocked")
    rows = await safety_events.find({}).to_list(5)
    assert rows and all("ABCDE1234F" not in (r["excerpt"] + r["original_text"]) and "98765" not in (r["excerpt"] + r["original_text"]) for r in rows)


# ---- brand contacts reach the check -------------------------------------------------------------------------------------------
async def test_the_brand_profile_supplies_its_own_contacts():
    from app.db.mongo import brand_profiles

    await brand_profiles.insert_one({"id": "b-contacts", "workspace_id": "w1", "website": "https://www.shop.in", "identity": {"about": "Reach us on 98765 43210"}})
    try:
        keys = await guard_agent.brand_contact_keys("b-contacts")
        assert "tel:9876543210" in keys and "dom:shop.in" in keys
        ok = await review_text("Call us on 98765 43210 today", where="generation", brand_id="b-contacts")
        assert ok.outcome == "clean"
        held = await review_text("Call us on 91234 56789 today", where="generation", brand_id="b-contacts", allow_rewrite=False)
        assert held.outcome == "blocked" and "personal_data" in held.categories
    finally:
        await brand_profiles.delete_many({"id": "b-contacts"})
    assert await guard_agent.brand_contact_keys(None) == frozenset()


# ---- languages the rules cannot read go to the model read -----------------------------------------------------------------
def test_a_post_in_another_script_is_always_worth_a_model_read():
    assert "language: not in Latin letters" in risk_signals("यह हमारी नई सेवा है, आज ही जुड़ें")
    assert "language: not in Latin letters" in risk_signals("எங்கள் புதிய சேவை இன்று தொடங்குகிறது")
    assert not any(s.startswith("language") for s in risk_signals("Our new service starts today"))
    assert not any(s.startswith("language") for s in risk_signals("Naalaiku offer irukku, vaanga"))


async def test_a_hindi_post_gets_the_model_read_in_risky_mode(monkeypatch):
    await guard_config.save({"model_check": "risky"}, expected_version=0, user_id="u")
    seen = []

    async def verdict(text):
        seen.append(text)
        from app.agents.content_guard.rules import ScreenResult
        return ScreenResult()

    monkeypatch.setattr(guard_agent, "_model_screen", verdict)
    await review_text("यह हमारी नई सेवा है, आज ही जुड़ें", where="generation")
    assert seen, "rules cannot read this script, so the model must"
    seen.clear()
    await review_text("Our new service starts today", where="generation")
    assert not seen


def test_the_model_question_covers_claims_personal_details_and_any_language():
    from app.prompts.registry import render_fragment

    question = render_fragment("content_guard/screen", allowed="claims, personal_data", post="x")
    assert "promise the brand cannot back up" in question and "business's own public contact details are fine" in question
    assert "any language or script" in question
