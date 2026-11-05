"""Asking for a review of a hold, the staff answer, the override that lets exactly those words go out, the rule nobody may judge for
themselves, the change history and the rule results. No model is called."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.agents.content_guard import appeals
from app.agents.content_guard import config as guard_config
from app.agents.content_guard.rules import content_hash
from app.core import staff as ST
from app.core.config import settings
from app.db.mongo import content_pieces, content_safety, get_db, safety_events, users
from app.pipelines.publish.spine import check_gate, unsafe_reason
from tests.conftest import create_workspace

THREAT = "I will kill you"


@pytest.fixture(autouse=True)
async def clean_state(monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUARD_LIVE_CHECKS", False)
    for col in (content_safety, safety_events, appeals.appeals_col(), appeals.overrides_col(), guard_config.history_col()):
        await col.delete_many({})
    guard_config.reset_cache()
    told = []

    async def record(**kwargs):
        told.append(kwargs)

    monkeypatch.setattr("app.shared.activity.projector.record_system", record)
    yield told
    for col in (content_safety, safety_events, appeals.appeals_col(), appeals.overrides_col(), guard_config.history_col()):
        await col.delete_many({})


async def _master(signup_user, name):
    client, profile = await signup_user(name)
    await users.update_one({"id": profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    ST.forget()
    return client, profile


async def _held_piece(workspace_id, user_id, text=THREAT, **extra):
    piece_id = f"ap-{uuid4().hex}"
    doc = {
        "piece_id": piece_id, "workspace_id": workspace_id, "user_id": user_id, "brand_id": "b1", "platform": "LinkedIn", "content": text,
        "flagged_for_review": True, "quality_passed": False, "quality_issues": ["May contain violence or threats."], "language": "en",
        "safety_reason": {"categories": ["Violence or threats"], "matches": ["kill you"], "message": "held"}, "deleted": False,
        "created_at": datetime.now(timezone.utc), "approval_status": "approved", "media": [],
    }
    doc.update(extra)
    await content_pieces.insert_one(doc)
    return piece_id


# ---- the member asks -------------------------------------------------------------------------------------------------------------
async def test_a_member_can_ask_for_a_review_of_a_held_post_once(signup_user):
    owner, profile = await signup_user("Owner")
    ws = await create_workspace(owner, f"Review {uuid4().hex[:6]}")
    piece_id = await _held_piece(ws, profile["id"])
    headers = {"X-Workspace-ID": ws}
    asked = await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id, "note": "It is a line from our film trailer"}, headers=headers)
    assert asked.status_code == 200, asked.text
    body = asked.json()
    assert body["status"] == "open" and body["categories"] == ["violence"] and body["category_names"] == ["Violence or threats"]
    again = await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id}, headers=headers)
    assert again.status_code == 409 and "already waiting" in again.json()["detail"]
    shown = (await owner.get(f"/api/v1/content-guard/review-requests/{piece_id}", headers=headers)).json()
    assert shown["request"]["status"] == "open"


async def test_a_post_that_is_not_held_has_nothing_to_review(signup_user):
    owner, profile = await signup_user("Owner")
    ws = await create_workspace(owner, f"Clean {uuid4().hex[:6]}")
    piece_id = await _held_piece(ws, profile["id"], text="A calm post", flagged_for_review=False, safety_reason=None)
    res = await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id}, headers={"X-Workspace-ID": ws})
    assert res.status_code == 409
    missing = await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": "nope"}, headers={"X-Workspace-ID": ws})
    assert missing.status_code == 404


async def test_one_workspace_cannot_ask_about_another_workspaces_post(signup_user):
    owner, profile = await signup_user("Owner")
    other, other_profile = await signup_user("Other")
    ws = await create_workspace(owner, f"Mine {uuid4().hex[:6]}")
    other_ws = await create_workspace(other, f"Theirs {uuid4().hex[:6]}")
    piece_id = await _held_piece(other_ws, other_profile["id"])
    res = await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id}, headers={"X-Workspace-ID": ws})
    assert res.status_code == 404


# ---- staff answer -----------------------------------------------------------------------------------------------------------------
async def test_releasing_lets_exactly_those_words_go_out(signup_user, clean_state):
    owner, profile = await signup_user("Owner")
    staff, _ = await _master(signup_user, "Founder")
    ws = await create_workspace(owner, f"Release {uuid4().hex[:6]}")
    piece_id = await _held_piece(ws, profile["id"])
    asked = (await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id, "note": "Film line"}, headers={"X-Workspace-ID": ws})).json()

    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert unsafe_reason(piece), "held before the answer"
    queue = (await staff.get("/api/v1/ops/content-safety/appeals?status=open")).json()
    assert queue["open"] >= 1 and any(a["id"] == asked["id"] for a in queue["items"])

    thin = await staff.post(f"/api/v1/ops/content-safety/appeals/{asked['id']}/decide", json={"decision": "release", "note": ""})
    assert thin.status_code == 409 and "few words" in thin.json()["detail"]
    done = await staff.post(f"/api/v1/ops/content-safety/appeals/{asked['id']}/decide", json={"decision": "release", "note": "A quote from the film, not a threat"})
    assert done.status_code == 200 and done.json()["status"] == "released"

    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert piece["flagged_for_review"] is False and piece["quality_passed"] is True and "safety_reason" not in piece
    assert unsafe_reason(piece) is None and check_gate(piece) is None, "the released words can go out"
    assert (await appeals.overrides_col().count_documents({})) == 1
    assert any(t["metadata"]["status"] == "released" for t in clean_state), "the member is told in Activity"

    edited = {**piece, "content": "I will kill you and your family"}
    assert unsafe_reason(edited), "a release never covers new words"
    again = await staff.post(f"/api/v1/ops/content-safety/appeals/{asked['id']}/decide", json={"decision": "uphold", "note": "Changed my mind"})
    assert again.status_code == 409 and "already answered" in again.json()["detail"]


async def test_upholding_keeps_the_hold(signup_user):
    owner, profile = await signup_user("Owner")
    staff, _ = await _master(signup_user, "Founder")
    ws = await create_workspace(owner, f"Uphold {uuid4().hex[:6]}")
    piece_id = await _held_piece(ws, profile["id"])
    asked = (await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id}, headers={"X-Workspace-ID": ws})).json()
    done = await staff.post(f"/api/v1/ops/content-safety/appeals/{asked['id']}/decide", json={"decision": "uphold", "note": "It reads as a real threat"})
    assert done.json()["status"] == "upheld"
    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert unsafe_reason(piece) and piece["flagged_for_review"] is True
    assert (await appeals.overrides_col().count_documents({})) == 0


async def test_only_staff_who_may_approve_safety_can_answer(signup_user):
    owner, profile = await signup_user("Owner")
    await ST.ensure_templates()
    analyst, analyst_profile = await signup_user("Analyst")
    await users.update_one({"id": analyst_profile["id"]}, {"$set": {"is_platform_staff": True, "staff": {"level": "staff", "role_id": "quality_analyst", "status": "active"}}})
    ST.forget()
    ws = await create_workspace(owner, f"Perm {uuid4().hex[:6]}")
    piece_id = await _held_piece(ws, profile["id"])
    asked = (await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id}, headers={"X-Workspace-ID": ws})).json()
    assert (await analyst.get("/api/v1/ops/content-safety/appeals")).status_code == 200, "can look"
    refused = await analyst.post(f"/api/v1/ops/content-safety/appeals/{asked['id']}/decide", json={"decision": "release", "note": "Looks fine to me"})
    assert refused.status_code == 403
    assert (await owner.get("/api/v1/ops/content-safety/appeals")).status_code == 403, "a customer cannot see the queue"


async def test_nobody_answers_a_review_about_a_rule_they_changed(signup_user):
    owner, profile = await signup_user("Owner")
    author, _ = await _master(signup_user, "Rule author")
    other, _ = await _master(signup_user, "Second admin")
    saved = await author.put("/api/v1/ops/content-safety", json={"version": 0, "extra_blocked_terms": ["crypto-scam"], "reason": "Block this scam term"})
    assert saved.status_code == 200, saved.text
    ws = await create_workspace(owner, f"Conflict {uuid4().hex[:6]}")
    piece_id = await _held_piece(ws, profile["id"], text="Try our crypto-scam today", safety_reason={"categories": ["A term blocked by Ekavo staff"], "matches": ["crypto-scam"], "message": "held"})
    asked = (await owner.post("/api/v1/content-guard/review-requests", json={"piece_id": piece_id}, headers={"X-Workspace-ID": ws})).json()
    blocked = await author.post(f"/api/v1/ops/content-safety/appeals/{asked['id']}/decide", json={"decision": "release", "note": "It is fine really"})
    assert blocked.status_code == 409 and "someone else" in blocked.json()["detail"]
    ok = await other.post(f"/api/v1/ops/content-safety/appeals/{asked['id']}/decide", json={"decision": "uphold", "note": "A real scam word"})
    assert ok.status_code == 200


# ---- change history ---------------------------------------------------------------------------------------------------------------
async def test_every_settings_change_needs_a_reason_and_is_kept(signup_user):
    staff, _ = await _master(signup_user, "Founder")
    none = await staff.put("/api/v1/ops/content-safety", json={"version": 0, "strictness": "strict"})
    assert none.status_code == 422
    saved = await staff.put("/api/v1/ops/content-safety", json={"version": 0, "strictness": "strict", "allowed_terms": ["nude lipstick"], "reason": "Cosmetics brands need this"})
    assert saved.status_code == 200
    rows = (await staff.get("/api/v1/ops/content-safety/history")).json()["items"]
    assert rows[0]["reason"] == "Cosmetics brands need this"
    paths = {c["path"]: c for c in rows[0]["changes"]}
    assert paths["strictness"]["before"] == "standard" and paths["strictness"]["after"] == "strict"
    assert paths["allowed_terms"]["added"] == ["nude lipstick"]


async def test_a_settings_change_can_be_undone(signup_user):
    staff, _ = await _master(signup_user, "Founder")
    await staff.put("/api/v1/ops/content-safety", json={"version": 0, "strictness": "strict", "reason": "Trying strict"})
    row = (await staff.get("/api/v1/ops/content-safety/history")).json()["items"][0]
    undone = await staff.post(f"/api/v1/ops/content-safety/history/{row['id']}/restore", json={"version": 1})
    assert undone.status_code == 200 and undone.json()["strictness"] == "standard"
    items = (await staff.get("/api/v1/ops/content-safety/history")).json()["items"]
    assert items[0]["reason"] == "Undid an earlier change" and len(items) == 2
    stale = await staff.post(f"/api/v1/ops/content-safety/history/{row['id']}/restore", json={"version": 0})
    assert stale.status_code == 409


# ---- how the rules perform --------------------------------------------------------------------------------------------------------
async def test_the_results_show_which_check_staff_keep_releasing():
    now = datetime.now(timezone.utc)
    await safety_events.insert_many([
        {"outcome": "blocked", "categories": ["violence"], "occurrences": 10, "created_at": now, "fingerprint": "f1"},
        {"outcome": "blocked", "categories": ["claims"], "occurrences": 4, "created_at": now, "fingerprint": "f2"},
    ])
    rows = []
    for category, status in (("violence", "released"), ("violence", "released"), ("violence", "upheld"), ("claims", "upheld"), ("claims", "open")):
        rows.append({"id": uuid4().hex, "categories": [category], "status": status, "created_at": now})
    await appeals.appeals_col().insert_many(rows)
    results = await appeals.rule_results(30)
    by = {r["category"]: r for r in results["rows"]}
    assert by["violence"]["held"] == 10 and by["violence"]["asked"] == 3 and by["violence"]["released"] == 2 and by["violence"]["released_share"] == 66.7
    assert by["claims"]["released_share"] == 0.0 and by["claims"]["answered"] == 1
    assert results["rows"][0]["category"] == "violence", "the check staff release most is listed first"


async def test_a_customer_cannot_read_overrides_results_or_history(signup_user):
    member, _ = await signup_user("Customer")
    for path in ("/overrides", "/rule-results", "/history", "/judges"):
        assert (await member.get(f"/api/v1/ops/content-safety{path}")).status_code == 403
