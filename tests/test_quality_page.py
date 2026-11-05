"""Ops, Quality: the starting values, what is refused, that the checks really read what Ops saves, the history and undo, the held
list, the try-a-text route, and who may change what. No model is called."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core import staff as ST
from app.db.mongo import get_db, users
from app.models.text import Platform
from app.pipelines.text import quality as text_quality
from app.quality import defaults as D
from app.quality import held
from app.quality import settings as QS


@pytest.fixture(autouse=True)
async def _clean_settings():
    await get_db()["quality_settings"].delete_many({})
    await get_db()["quality_history"].delete_many({})
    QS.reset_cache()
    yield
    await get_db()["quality_settings"].delete_many({})
    await get_db()["quality_history"].delete_many({})
    QS.reset_cache()


async def _master(signup_user, name="Founder"):
    client, profile = await signup_user(name)
    await users.update_one({"id": profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    ST.forget()
    return client, profile


async def _analyst(signup_user):
    await ST.ensure_templates()
    client, profile = await signup_user("Analyst")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_platform_staff": True, "staff": {"level": "staff", "role_id": "quality_analyst", "status": "active"}}})
    ST.forget()
    return client, profile


def _words(n: int) -> str:
    return " ".join(["plain"] * n) + "."


# ---- starting values -------------------------------------------------------------------------------------------------------------------
def test_the_starting_values_are_the_ones_the_code_used_before():
    assert text_quality.MIN_WORD_COUNTS[Platform.BLOG] == 600 and text_quality.MIN_WORD_COUNTS[Platform.TWITTER] == 0
    assert text_quality.READABILITY_THRESHOLDS[Platform.LINKEDIN] == 55
    assert QS.min_words("LinkedIn") == 150 and QS.readability_bar("Instagram") == 65
    assert QS.trust()["min_decided"] == 10 and QS.patterns()["min_ratio"] == 1.5
    assert abs(sum(QS.trust_weights().values()) - 1) < 1e-9
    assert QS.edited_paths() == []


# ---- what is refused -------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("changes, words", [
    ({"min_words": {"LinkedIn": 2001}}, "between 0 and 2000"),
    ({"min_words": {"LinkedIn": 12.5}}, "whole number"),
    ({"min_words": {"Myspace": 10}}, "not a platform"),
    ({"readability": {"Blog": 101}}, "between 0 and 100"),
    ({"readability": {"Blog": "high"}}, "must be a number"),
    ({"cta_markers": []}, "at least one"),
    ({"passive_patterns": ["(a+)+b"]}, "very slow"),
    ({"passive_patterns": ["[unclosed"]}, "not a valid pattern"),
    ({"weasel_words": ["x" * 61]}, "longer than"),
    ({"patterns": {"min_ratio": 0.5}}, "between 1.1 and 10"),
    ({"patterns": {"nope": 1}}, "not a setting"),
    ({"trust": {"weight_kept": 30}}, "add up to 100"),
    ({"surprise": 1}, "not a setting"),
])
def test_bad_values_are_refused_with_plain_words(changes, words):
    with pytest.raises(QS.SettingsError) as err:
        QS.clean_changes(changes, QS.defaults())
    assert words in str(err.value)


def test_lists_are_tidied_and_weights_may_move_together():
    out = QS.clean_changes({"cta_markers": ["  Book A Call ", "book a call", "", "Reply"], "trust": {"weight_kept": 30, "weight_delivered": 15}}, QS.defaults())
    assert out["cta_markers"] == ["book a call", "reply"]
    assert out["trust"] == {"weight_kept": 30, "weight_delivered": 15}


# ---- the checks read what Ops saves ---------------------------------------------------------------------------------------------------
async def test_a_saved_minimum_changes_what_the_gate_holds():
    text = _words(120) + " What do you think?"
    held_before = await text_quality.run_quality_gate(text, Platform.LINKEDIN, "", [])
    assert not held_before.passed and any("too short" in i.lower() for i in held_before.issues)
    await QS.save({"min_words": {"LinkedIn": 100}}, reason="Good short posts were held", expected_version=0, user_id="u1")
    after = await text_quality.run_quality_gate(text, Platform.LINKEDIN, "", [])
    assert after.passed
    assert QS.edited_paths() == ["min_words.LinkedIn"]


async def test_a_saved_call_to_action_word_and_filler_word_are_used():
    plain = "A calm and clear note about the week. Thanks for reading it all."
    base = await text_quality.run_quality_gate(plain, Platform.FACEBOOK, "", [], grammar_check=True)
    assert any("No CTA" in i for i in base.issues)
    await QS.save({"cta_markers": ["thanks for reading"], "weasel_words": ["calm"]}, reason="Our own closing counts", expected_version=0, user_id="u1")
    after = await text_quality.run_quality_gate(plain, Platform.FACEBOOK, "", [], grammar_check=True)
    assert not any("No CTA" in i for i in after.issues)
    assert any("Weasel word" in i and "calm" in i for i in after.issues)


async def test_the_reading_bar_and_pattern_numbers_come_from_settings():
    from app.pipelines.text import scorer
    await QS.save({"readability": {"Blog": 90}, "patterns": {"min_ratio": 3}}, reason="Raise the blog bar", expected_version=0, user_id="u1")
    assert scorer._QS.readability_bar("Blog") == 90
    from app.agents.feedback import patterns
    assert patterns._compare([5, 5, 5], [2, 2, 2]) is None, "2.5 times is under the new 3 times"
    await QS.save({"patterns": {"min_ratio": 1.5}}, reason="Back again", expected_version=1, user_id="u1")
    assert patterns._compare([5, 5, 5], [2, 2, 2]) == 2.5


async def test_trust_numbers_reach_the_score(monkeypatch):
    await QS.save({"trust": {"weight_untouched": 50, "weight_kept": 20, "weight_quality": 15, "weight_delivered": 15}}, reason="Untouched matters most", expected_version=0, user_id="u1")
    weights = QS.trust_weights()
    assert weights["untouched"] == 0.5 and abs(sum(weights.values()) - 1) < 1e-9


# ---- saving, history, undo, conflicts -------------------------------------------------------------------------------------------------
async def test_a_save_needs_a_reason_and_keeps_old_and_new_in_history():
    with pytest.raises(QS.SettingsError):
        await QS.save({"min_words": {"Blog": 500}}, reason="", expected_version=0, user_id="u1")
    await QS.save({"min_words": {"Blog": 500}}, reason="Our blogs run shorter", expected_version=0, user_id="u1", user_name="Priya")
    rows = await QS.history()
    assert len(rows) == 1 and rows[0]["by_name"] == "Priya" and rows[0]["reason"] == "Our blogs run shorter"
    assert rows[0]["changes"] == [{"path": "min_words.Blog", "label": "Shortest post, Blog", "before": 600, "after": 500}]
    with pytest.raises(QS.SettingsError):
        await QS.save({"min_words": {"Blog": 500}}, reason="Same again", expected_version=1, user_id="u1")


async def test_undo_puts_the_old_value_back_and_is_itself_recorded():
    await QS.save({"readability": {"LinkedIn": 40}}, reason="Too many warnings", expected_version=0, user_id="u1")
    first = (await QS.history())[0]
    await QS.restore(first["id"], expected_version=1, user_id="u2", user_name="Arun")
    assert QS.readability_bar("LinkedIn") == 55 and QS.edited_paths() == []
    rows = await QS.history()
    assert [r["kind"] for r in rows] == ["restore", "save"]


async def test_two_people_saving_from_the_same_version_conflict():
    await QS.save({"min_words": {"Blog": 500}}, reason="First person", expected_version=0, user_id="u1")
    with pytest.raises(QS.VersionConflict):
        await QS.save({"min_words": {"Blog": 400}}, reason="Second person", expected_version=0, user_id="u2")
    assert QS.min_words("Blog") == 500


async def test_reset_returns_every_field_and_can_be_undone():
    await QS.save({"min_words": {"Blog": 500}, "cta_markers": ["book a call"]}, reason="Our own words", expected_version=0, user_id="u1")
    await QS.reset_all(reason="Start again", expected_version=1, user_id="u1")
    assert QS.edited_paths() == [] and "book a call" not in QS.cta_markers()
    last = (await QS.history())[0]
    await QS.restore(last["id"], expected_version=2, user_id="u1")
    assert QS.min_words("Blog") == 500


# ---- trying unsaved values --------------------------------------------------------------------------------------------------------------
async def test_trying_values_never_leaks_into_other_requests():
    trial = QS.defaults()
    trial["min_words"]["LinkedIn"] = 1
    with QS.trying(trial):
        assert QS.min_words("LinkedIn") == 1
    assert QS.min_words("LinkedIn") == 150


# ---- held posts -------------------------------------------------------------------------------------------------------------------------
def test_each_reason_lands_in_one_named_check():
    assert held.classify("Content too short: 90 words, minimum 150 for LinkedIn") == "too_short"
    assert held.classify("Banned word found: 'cheap'") == "banned_words"
    assert held.classify("The post is not written in the requested language and script.") == "language"
    assert held.classify("Twitter character limit exceeded: 300/280") == "length_limit"
    assert held.classify("Generic opening detected: 'in today's world'") == "opening_closing"
    assert held.classify("May contain violence.") == "safety"
    assert held.classify("Something new") == "other"
    assert held.reasons(["Advisory: Readability score 40 below 55 for Blog", "Content too short: 1"]) == [{"check": "too_short", "text": "Content too short: 1"}]


async def _piece(platform, passed, issues, days_ago=1, content="one two three", readability=60.0):
    await get_db()["content_pieces"].insert_one({
        "piece_id": f"q-{uuid4().hex}", "workspace_id": "w-quality-test", "platform": platform, "content": content, "quality_passed": passed,
        "quality_issues": issues, "readability_score": readability, "created_at": datetime.now(timezone.utc) - timedelta(days=days_ago),
    })


async def test_the_summary_counts_by_check_platform_and_day():
    await get_db()["content_pieces"].delete_many({"workspace_id": "w-quality-test"})
    await _piece("LinkedIn", True, [])
    await _piece("LinkedIn", False, ["Content too short: 90 words, minimum 150 for LinkedIn"])
    await _piece("LinkedIn", False, ["Content too short: 80 words, minimum 150 for LinkedIn", "Advisory: Readability score 40 below 55"])
    await _piece("Blog", False, ["Banned word found: 'cheap'"], days_ago=3)
    await _piece("Blog", False, ["Content too short: 1"], days_ago=200)  # outside the window
    s = await held.summary(30)
    mine = {c["check"]: c["count"] for c in s["by_check"]}
    assert mine["too_short"] >= 2 and mine["banned_words"] >= 1
    assert any(p["platform"] == "LinkedIn" and p["checks"].get("too_short", 0) >= 2 for p in s["by_platform"])
    assert len(s["daily"]) == 30 and sum(d["held"] for d in s["daily"]) == s["held"]
    listing = await held.held_list(days=30, platform="LinkedIn", check="too_short")
    assert listing["total"] >= 2 and all(r["platform"] == "LinkedIn" for r in listing["posts"])
    one = await held.held_one(listing["posts"][0]["id"])
    assert one["reasons"][0]["check"] == "too_short"
    await get_db()["content_pieces"].delete_many({"workspace_id": "w-quality-test"})


async def test_the_distribution_returns_the_values_so_a_bar_can_be_judged():
    await get_db()["content_pieces"].delete_many({"workspace_id": "w-quality-test"})
    for score, n in ((30.0, 40), (50.0, 80), (70.0, 200)):
        await _piece("YouTube", True, [], content=_words(n), readability=score)
    d = await held.distribution("YouTube", 30)
    assert d["posts"] >= 3 and 30 in d["reading"]["values"] and sum(d["reading"]["counts"]) == d["reading"]["measured"]
    assert len(d["words"]["counts"]) == len(d["words"]["edges"]) + 1
    await get_db()["content_pieces"].delete_many({"workspace_id": "w-quality-test"})


# ---- the routes and who may use them ----------------------------------------------------------------------------------------------------
async def test_a_customer_cannot_open_the_quality_routes(signup_user):
    member, _ = await signup_user("Customer")
    for path in ("", "/summary", "/history", "/held"):
        assert (await member.get(f"/api/v1/ops/quality{path}")).status_code == 403
    assert (await member.put("/api/v1/ops/quality", json={"changes": {}, "reason": "x", "version": 0})).status_code == 403


async def test_an_analyst_changes_the_writing_checks_but_not_the_trust_numbers(signup_user):
    analyst, _ = await _analyst(signup_user)
    page = (await analyst.get("/api/v1/ops/quality")).json()
    assert page["version"] == 0 and page["can_edit_trust"] is False and page["values"]["min_words"]["Blog"] == 600
    ok = await analyst.put("/api/v1/ops/quality", json={"changes": {"min_words": {"Blog": 500}}, "reason": "Our blogs run shorter", "version": 0})
    assert ok.status_code == 200 and ok.json()["values"]["min_words"]["Blog"] == 500 and ok.json()["edited"] == ["min_words.Blog"]
    blocked = await analyst.put("/api/v1/ops/quality", json={"changes": {"trust": {"min_decided": 5}}, "reason": "Faster scores", "version": 1})
    assert blocked.status_code == 403
    assert (await analyst.post("/api/v1/ops/quality/reset", json={"reason": "Start again", "version": 1})).status_code == 403
    stale = await analyst.put("/api/v1/ops/quality", json={"changes": {"min_words": {"Blog": 400}}, "reason": "Another change", "version": 0})
    assert stale.status_code == 409
    bad = await analyst.put("/api/v1/ops/quality", json={"changes": {"min_words": {"Blog": 5000}}, "reason": "Too much", "version": 1})
    assert bad.status_code == 422 and "between 0 and 2000" in bad.json()["detail"]


async def test_a_viewer_sees_the_page_but_cannot_change_it(signup_user):
    await ST.ensure_templates()
    client, profile = await signup_user("Safety person")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_platform_staff": True, "staff": {"level": "staff", "role_id": "trust_safety", "status": "active"}}})
    ST.forget()
    assert (await client.get("/api/v1/ops/quality")).status_code == 200
    assert (await client.put("/api/v1/ops/quality", json={"changes": {"min_words": {"Blog": 500}}, "reason": "Try it", "version": 0})).status_code == 403


async def test_a_master_admin_changes_trust_resets_and_undoes(signup_user):
    master, _ = await _master(signup_user)
    saved = await master.put("/api/v1/ops/quality", json={"changes": {"trust": {"min_decided": 20}}, "reason": "Wait for more drafts", "version": 0})
    assert saved.status_code == 200 and saved.json()["values"]["trust"]["min_decided"] == 20
    history = (await master.get("/api/v1/ops/quality/history")).json()
    assert history["items"][0]["reason"] == "Wait for more drafts"
    undone = await master.post(f"/api/v1/ops/quality/history/{history['items'][0]['id']}/restore", json={"version": 1})
    assert undone.status_code == 200 and undone.json()["values"]["trust"]["min_decided"] == 10
    again = await master.put("/api/v1/ops/quality", json={"changes": {"cta_markers": ["book a call"]}, "reason": "Our own phrase", "version": 2})
    assert again.status_code == 200
    reset = await master.post("/api/v1/ops/quality/reset", json={"reason": "Start again", "version": 3})
    assert reset.status_code == 200 and reset.json()["edited"] == []


async def test_try_a_text_uses_saved_or_unsaved_values_and_changes_nothing(signup_user):
    master, _ = await _master(signup_user)
    body = {"text": _words(120) + " What do you think?", "platform": "LinkedIn", "language": "en"}
    saved = (await master.post("/api/v1/ops/quality/try", json=body)).json()
    assert saved["passed"] is False and saved["using"] == "the saved values" and saved["minimum_words"] == 150
    assert any(c["state"] == "held" and c["check"] == "too_short" for c in saved["checks"])
    trial = (await master.post("/api/v1/ops/quality/try", json={**body, "changes": {"min_words": {"LinkedIn": 100}}})).json()
    assert trial["passed"] is True and trial["using"] == "your unsaved values" and trial["minimum_words"] == 100
    assert (await master.get("/api/v1/ops/quality")).json()["values"]["min_words"]["LinkedIn"] == 150, "trying saves nothing"
    assert (await master.post("/api/v1/ops/quality/try", json={**body, "platform": "Myspace"})).status_code == 422
    assert (await master.post("/api/v1/ops/quality/try", json={**body, "changes": {"min_words": {"LinkedIn": -1}}})).status_code == 422


async def test_the_analyst_role_and_area_exist():
    assert "quality" in ST.AREAS
    template = next(t for t in ST.TEMPLATES if t["key"] == "quality_analyst")
    assert template["permissions"]["quality"] == ["view", "edit"] and template["active"] is True
