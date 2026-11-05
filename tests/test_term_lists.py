"""Word lists for one language and the queue of suggested words: which list a post uses, that nothing goes live without approval, and that
a rejected word does not come back. No model is called."""
import pytest

from app.agents.content_guard import config as guard_config
from app.agents.content_guard import term_lists as T
from app.agents.content_guard.agent import rule_screen
from app.core import staff as ST
from app.db.mongo import content_safety, users


@pytest.fixture(autouse=True)
async def clean():
    for col in (content_safety, T.col(), guard_config.history_col()):
        await col.delete_many({})
    guard_config.reset_cache()
    yield
    for col in (content_safety, T.col(), guard_config.history_col()):
        await col.delete_many({})


async def _master(signup_user, name="Founder"):
    client, profile = await signup_user(name)
    await users.update_one({"id": profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    ST.forget()
    return client, profile


# ---- the lists --------------------------------------------------------------------------------------------------------------------
def test_lists_are_tidied_per_language_and_any_code_is_allowed():
    tidy = T.tidy_by_language({"TA-IN": {"blocked": [" Word One ", "word one", ""], "allowed": ["Fine"]}, "xx": {"blocked": ["a b"]}, "empty": {"blocked": []}, "bad": "text"})
    assert tidy == {"ta": {"blocked": ["word one"], "allowed": ["fine"]}, "xx": {"blocked": ["a b"], "allowed": []}}
    assert T.tidy_by_language("garbage") == {}
    assert len(T.tidy_by_language({f"l{i}": {"blocked": ["x"]} for i in range(100)})) == T.LANGUAGES_MAX


def test_codes_follow_the_language_asked_for_or_the_script_shown():
    assert T.codes_for("anything", "ta+en") == ["ta", "en"]
    assert T.codes_for("anything", "hi-IN") == ["hi"]
    assert T.codes_for("यह हमारी नई सेवा है", None) == ["hi"]
    assert T.codes_for("", None) == []


def test_a_post_uses_its_languages_list_and_the_main_one():
    cfg = {"extra_blocked_terms": ["scam"], "allowed_terms": [], "terms_by_language": {"ta": {"blocked": ["போலி"], "allowed": []}, "hi": {"blocked": ["धोखा"], "allowed": []}}}
    tamil = T.apply(cfg, "இது போலி விளம்பரம்", "ta")
    assert tamil["extra_blocked_terms"] == ["scam", "போலி"]
    assert T.apply(cfg, "यह धोखा है", None)["extra_blocked_terms"] == ["scam", "धोखा"]
    assert T.apply(cfg, "plain english", "en")["extra_blocked_terms"] == ["scam"]
    assert T.apply({"extra_blocked_terms": ["x"]}, "t", "ta") == {"extra_blocked_terms": ["x"]}


async def test_the_screen_holds_a_languages_word_only_in_that_language():
    await guard_config.save({"terms_by_language": {"ta": {"blocked": ["போலி"], "allowed": []}}}, expected_version=0, user_id="u", reason="Tamil word")
    assert not rule_screen("இது போலி விளம்பரம்", language="ta").ok
    assert not rule_screen("இது போலி விளம்பரம்").ok, "found from the script when no language was given"
    assert rule_screen("இது போலி விளம்பரம்", language="hi") is not None
    assert rule_screen("a fake word போலி here", language="en").ok, "an English post does not use the Tamil list"


async def test_a_languages_allowed_word_protects_it_only_there():
    await guard_config.save({"extra_blocked_terms": ["nude"], "terms_by_language": {"ta": {"blocked": [], "allowed": ["nude"]}}}, expected_version=0, user_id="u", reason="Allow it")
    assert rule_screen("nude lipstick shade இது", language="ta").ok
    assert not rule_screen("nude lipstick shade", language="en").ok


# ---- the queue ----------------------------------------------------------------------------------------------------------------------
async def test_a_suggestion_is_queued_once_and_counts_how_often_it_came_up():
    first = await T.suggest(" Fake  Offer ", "ta-IN", source="review", by="u1", example="Call 98765 43210 for the fake offer")
    again = await T.suggest("fake offer", "ta", source="staff", by="u2")
    assert first["id"] == again["id"] and first["term"] == "fake offer" and first["language"] == "ta"
    row = await T.col().find_one({"term": "fake offer"})
    assert row["evidence"] == 2 and "98765" not in row["example"]
    with pytest.raises(T.SuggestionError):
        await T.suggest("x", "ta", source="review", by="u")
    with pytest.raises(T.SuggestionError):
        await T.suggest("fine word", "ta", source="made-up", by="u")


async def test_a_pasted_list_is_queued_never_added_to_the_rules():
    result = await T.import_seeds(["one word", "two word", "one word", "z"], "hi", by="u")
    assert result["added"] == 2 and result["skipped"] == 1
    assert (await guard_config.load()).get("terms_by_language") == {}
    assert (await T.listing("pending"))["pending"] == 2


async def test_accepting_adds_the_word_as_a_recorded_change(signup_user):
    staff, profile = await _master(signup_user)
    row = await T.suggest("போலி", "ta", source="review", by="u1")
    done = await staff.post(f"/api/v1/ops/content-safety/suggestions/{row['id']}/decide", json={"decision": "accept"})
    assert done.status_code == 200 and done.json()["status"] == "accepted"
    assert (await guard_config.load())["terms_by_language"]["ta"]["blocked"] == ["போலி"]
    assert not rule_screen("இது போலி விளம்பரம்", language="ta").ok
    history = (await staff.get("/api/v1/ops/content-safety/history")).json()["items"]
    assert "Accepted the suggested word" in history[0]["reason"] and history[0]["by"] == profile["id"]
    again = await staff.post(f"/api/v1/ops/content-safety/suggestions/{row['id']}/decide", json={"decision": "reject"})
    assert again.status_code == 409


async def test_a_word_for_every_language_goes_to_the_main_list(signup_user):
    staff, _ = await _master(signup_user)
    row = await T.suggest("crypto-scam", "all", source="staff", by="u1")
    await staff.post(f"/api/v1/ops/content-safety/suggestions/{row['id']}/decide", json={"decision": "accept"})
    assert "crypto-scam" in (await guard_config.load())["extra_blocked_terms"]


async def test_a_rejected_word_is_remembered_and_not_queued_again(signup_user):
    staff, _ = await _master(signup_user)
    row = await T.suggest("fine word", "hi", source="review", by="u1")
    await staff.post(f"/api/v1/ops/content-safety/suggestions/{row['id']}/decide", json={"decision": "reject"})
    again = await T.suggest("fine word", "hi", source="seed", by="u2")
    assert again["status"] == "rejected" and (await T.listing("pending"))["pending"] == 0
    assert "fine word" not in (((await guard_config.load()).get("terms_by_language") or {}).get("hi") or {}).get("blocked", [])


async def test_upholding_a_review_can_suggest_the_word_that_caused_it(signup_user):
    from uuid import uuid4

    from app.agents.content_guard import appeals
    from app.db.mongo import content_pieces
    from tests.conftest import create_workspace

    owner, profile = await signup_user("Owner")
    staff, _ = await _master(signup_user)
    ws = await create_workspace(owner, f"Suggest {uuid4().hex[:6]}")
    piece_id = f"t-{uuid4().hex}"
    await content_pieces.insert_one({"piece_id": piece_id, "workspace_id": ws, "user_id": profile["id"], "platform": "LinkedIn", "content": "I will kill you", "flagged_for_review": True,
                                     "safety_reason": {"categories": ["Violence or threats"], "matches": ["kill you"], "message": "held"}, "language": "hi", "deleted": False})
    asked = (await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id}, headers={"X-Workspace-Id": ws})).json()
    done = await staff.post(f"/api/v1/ops/content-safety/appeals/{asked['id']}/decide", json={"decision": "uphold", "note": "A real threat", "suggest_word": "kill you"})
    assert done.status_code == 200
    queue = (await staff.get("/api/v1/ops/content-safety/suggestions")).json()
    assert queue["pending"] == 1 and queue["items"][0]["term"] == "kill you" and queue["items"][0]["language"] == "hi" and queue["items"][0]["source"] == "review"
    await appeals.appeals_col().delete_many({"piece_id": piece_id})
    await content_pieces.delete_many({"piece_id": piece_id})


async def test_only_staff_with_approval_rights_answer_and_customers_see_nothing(signup_user):
    await ST.ensure_templates()
    analyst, profile = await signup_user("Analyst")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_platform_staff": True, "staff": {"level": "staff", "role_id": "quality_analyst", "status": "active"}}})
    ST.forget()
    row = await T.suggest("some word", "ta", source="staff", by="u")
    assert (await analyst.get("/api/v1/ops/content-safety/suggestions")).status_code == 200
    assert (await analyst.post(f"/api/v1/ops/content-safety/suggestions/{row['id']}/decide", json={"decision": "accept"})).status_code == 403
    customer, _ = await signup_user("Customer")
    assert (await customer.get("/api/v1/ops/content-safety/suggestions")).status_code == 403
