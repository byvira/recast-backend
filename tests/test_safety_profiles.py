"""Brand safety profiles: what each adds, that none can loosen the standard checks, that a brand picks one, and that the check and the
publish gate honour it. No real model is called."""
from unittest.mock import AsyncMock

import pytest

from app.agents.content_guard import agent as guard_agent
from app.agents.content_guard import config as guard_config
from app.agents.content_guard import profiles as P
from app.agents.content_guard.agent import check_piece_before_send, guard_piece_doc, review_text, rule_screen
from app.agents.content_guard.rules import ScreenResult
from app.core.config import settings
from app.db.mongo import brand_profiles, content_safety, safety_events
from app.pipelines.publish.spine import unsafe_reason
from tests.conftest import create_workspace

STRONG = "This is bullshit and we are done"


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
    await brand_profiles.delete_many({"id": {"$regex": "^b-prof"}})


# ---- what a profile adds -------------------------------------------------------------------------------------------------------
def test_a_profile_only_adds_care():
    cfg = {"strictness": "standard", "extra_blocked_terms": ["scam"], "profiles": {"health": {"terms": ["Miracle Tea"], "note": "", "strict": False}}}
    assert P.effective(cfg, "general") is cfg
    kids = P.effective(cfg, "kids")
    assert kids["strictness"] == "strict" and kids["extra_blocked_terms"] == ["scam"], "kids is strict out of the box"
    health = P.effective(cfg, "health")
    assert health["strictness"] == "standard" and health["extra_blocked_terms"] == ["scam", "Miracle Tea"]
    assert P.effective({"strictness": "strict"}, "finance")["strictness"] == "strict", "a strict workspace stays strict"
    assert P.effective(cfg, "no-such-profile") is cfg


def test_ops_cannot_loosen_a_profile():
    cfg = {"profiles": {"kids": {"terms": [], "note": "", "strict": False}}}
    assert P.effective(cfg, "kids")["strictness"] == "strict", "kids stays strict whatever Ops saved"
    assert P.always_read("kids") and not P.always_read("general") and not P.always_read(None)


def test_ops_edits_are_tidied_and_unknown_profiles_dropped():
    tidy = P.tidy_overrides({"kids": {"terms": [" Candy Scam ", "candy scam", ""], "note": "  a   b  ", "strict": 1}, "general": {"terms": ["x"]}, "nope": {"terms": ["y"]}, "health": {"terms": [], "note": "", "strict": False}})
    assert tidy == {"kids": {"terms": ["candy scam"], "note": "a b", "strict": True}}
    assert P.tidy_overrides("garbage") == {}


def test_the_note_is_ops_own_wording_when_set_and_the_starting_one_otherwise():
    assert "treats, cures or prevents" in P.note({}, "health")
    assert P.note({"profiles": {"health": {"note": "Our own words", "terms": [], "strict": False}}}, "health") == "Our own words"
    assert P.note({}, "general") == "" and P.note({}, None) == ""


# ---- the rules honour the profile -----------------------------------------------------------------------------------------------
def test_strong_language_is_held_for_a_childrens_brand_only():
    assert rule_screen(STRONG).ok
    assert rule_screen(STRONG, profile="general").ok
    held = rule_screen(STRONG, profile="kids")
    assert not held.ok and "profanity" in held.categories


async def test_terms_ops_adds_to_a_profile_are_blocked_for_that_profile_only():
    await guard_config.save({"profiles": {"health": {"terms": ["miracle tea"], "note": "", "strict": False}}}, expected_version=0, user_id="u")
    assert not rule_screen("Try our miracle tea today", profile="health").ok
    assert rule_screen("Try our miracle tea today", profile="general").ok
    assert rule_screen("Try our miracle tea today").ok


# ---- the model read ---------------------------------------------------------------------------------------------------------------
async def test_a_profile_always_gets_the_model_read_and_the_model_is_told_the_brand_policy(monkeypatch):
    await guard_config.save({"model_check": "risky"}, expected_version=0, user_id="u")
    await brand_profiles.insert_one({"id": "b-prof-health", "workspace_id": "w1", "user_id": "u", "safety_profile": "health"})
    await brand_profiles.insert_one({"id": "b-prof-plain", "workspace_id": "w1", "user_id": "u"})
    seen = []

    async def read(judge, text):
        seen.append(guard_agent._policy_note.get())
        return ScreenResult()

    monkeypatch.setattr(guard_agent, "_read_with", read)
    await review_text("Our herbal tea tastes lovely", where="generation", brand_id="b-prof-health")
    assert seen and "treats, cures or prevents" in seen[0]
    seen.clear()
    await review_text("Our herbal tea tastes lovely too", where="generation", brand_id="b-prof-plain")
    assert not seen, "a general brand with a calm text stays on the free rules"


def test_the_model_question_carries_the_policy_note():
    from app.prompts.registry import render_fragment

    with_note = render_fragment("content_guard/screen", allowed="claims", policy_note="No miracle cures.", post="x")
    without = render_fragment("content_guard/screen", allowed="claims", policy_note="", post="x")
    assert "Extra rules for this brand: No miracle cures." in with_note and "Extra rules" not in without


# ---- on the post and at the gate -----------------------------------------------------------------------------------------------------
async def test_the_piece_remembers_the_profile_and_the_gate_honours_it():
    await guard_config.save({"rewrite_flagged": False}, expected_version=0, user_id="u")
    await brand_profiles.insert_one({"id": "b-prof-kids", "workspace_id": "w1", "user_id": "u", "safety_profile": "kids", "language": "ta+en"})
    doc = {"content": STRONG, "workspace_id": "w1", "piece_id": "p-kids", "platform": "LinkedIn", "brand_id": "b-prof-kids"}
    out = await guard_piece_doc(doc, where="generation")
    assert out["safety_policy"] == {"profile": "kids"} and out["language"] == "ta+en", "the brand's language fills a gap"
    assert out["flagged_for_review"] is True, "held for a children's brand"
    assert unsafe_reason({"content": STRONG, "safety_policy": {"profile": "kids"}})
    assert unsafe_reason({"content": STRONG, "safety_policy": {"profile": "general"}}) is None
    assert unsafe_reason({"content": STRONG}) is None


async def test_the_read_before_sending_uses_the_pieces_profile(monkeypatch):
    await guard_config.save({"model_check": "publish"}, expected_version=0, user_id="u")
    notes = []

    async def read(judge, text):
        notes.append(guard_agent._policy_note.get())
        return ScreenResult()

    monkeypatch.setattr(guard_agent, "_read_with", read)
    from app.db.mongo import content_pieces

    await content_pieces.insert_one({"piece_id": "p-fin", "workspace_id": "w1", "content": "Our fund is open to everyone", "safety_policy": {"profile": "finance"}})
    try:
        await check_piece_before_send({"piece_id": "p-fin", "workspace_id": "w1", "content": "Our fund is open to everyone", "safety_policy": {"profile": "finance"}}, "w1")
    finally:
        await content_pieces.delete_many({"piece_id": "p-fin"})
    assert notes and "guaranteed or high returns" in notes[0]


# ---- choosing it ---------------------------------------------------------------------------------------------------------------------
async def test_a_member_picks_a_profile_for_their_brand_and_a_bad_name_is_refused(signup_user):
    owner, profile = await signup_user("Owner")
    ws = await create_workspace(owner, "Profile workspace")
    await brand_profiles.insert_one({"id": "b-prof-pick", "workspace_id": ws, "user_id": profile["id"]})
    headers = {"X-Workspace-Id": ws}
    ok = await owner.patch("/api/v1/brand/b-prof-pick/safety-profile", json={"profile": "finance"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["safety_profile"] == "finance"
    assert (await brand_profiles.find_one({"id": "b-prof-pick"}))["safety_profile"] == "finance"
    assert (await owner.patch("/api/v1/brand/b-prof-pick/safety-profile", json={"profile": "weird"}, headers=headers)).status_code == 422
    cleared = await owner.patch("/api/v1/brand/b-prof-pick/safety-profile", json={"profile": ""}, headers=headers)
    assert cleared.json()["safety_profile"] is None
    assert (await owner.patch("/api/v1/brand/nope/safety-profile", json={"profile": "kids"}, headers=headers)).status_code == 404


async def test_ops_sees_each_profile_next_to_its_edits(signup_user):
    from app.core import staff as ST
    from app.db.mongo import users

    client, profile = await signup_user("Founder")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    ST.forget()
    saved = await client.put("/api/v1/ops/content-safety", json={"version": 0, "profiles": {"kids": {"terms": ["Candy Scam"], "note": "", "strict": False}}, "reason": "Add a term for kids brands"})
    assert saved.status_code == 200
    defs = {d["key"]: d for d in saved.json()["profile_defs"]}
    assert set(defs) == set(P.PROFILES) and defs["kids"]["terms"] == ["candy scam"] and defs["kids"]["strict"] is True
    assert defs["health"]["default_note"].startswith("A claim that a product")
