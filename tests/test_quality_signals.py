"""What members tell us about what Ekavo writes: the reasons they tap, how much they change what they keep, the nightly counting and the
Ops view. No model is called."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core import staff as ST
from app.db.mongo import content_piece_versions, content_pieces, users
from app.quality import signals as S
from tests.conftest import create_workspace

WORDS = " ".join(f"word{i}" for i in range(40))


def _one_changed() -> str:
    parts = WORDS.split()
    parts[3] = "changed"
    return " ".join(parts)


@pytest.fixture(autouse=True)
async def clean():
    for col in (S.feedback_col(), S.daily_col()):
        await col.delete_many({})
    yield
    for col in (S.feedback_col(), S.daily_col()):
        await col.delete_many({})
    await content_pieces.delete_many({"workspace_id": "w-signal"})
    await content_piece_versions.delete_many({"workspace_id": "w-signal"})


# ---- the edit signal ---------------------------------------------------------------------------------------------------------------
def test_edit_class_counts_the_share_of_words_that_changed():
    assert S.edit_class(WORDS, WORDS) == "untouched"
    assert S.edit_class(WORDS, "  " + WORDS + "  ") == "untouched", "spaces are not edits"
    assert S.edit_class(WORDS, _one_changed()) == "light", "one word in forty"
    assert S.edit_class(WORDS, " ".join(f"other{i}" for i in range(40))) == "heavy"
    assert S.edit_class(WORDS, "") == "heavy" and S.edit_class("", WORDS) == "heavy"
    swapped = WORDS.split()
    swapped[:6] = ["new1", "new2", "new3", "new4", "new5", "new6"]
    assert S.edit_class(WORDS, " ".join(swapped)) == "heavy", "six words of forty is 15%"


def test_edit_class_works_for_words_in_any_script():
    hindi = "यह हमारी नई सेवा है जो आज शुरू हो रही है और सबके लिए खुली है"
    assert S.edit_class(hindi, hindi) == "untouched"
    assert S.edit_class(hindi, hindi + " धन्यवाद") in ("light", "heavy")


# ---- reasons ---------------------------------------------------------------------------------------------------------------------------
async def test_a_reason_is_kept_with_the_posts_platform_and_language():
    row = await S.record_feedback({"piece_id": "p1", "workspace_id": "w-signal", "platform": "LinkedIn", "language": "hi", "version_count": 2}, kind="regenerated", reason="wrong_tone", user_id="u1", note="  too   formal ")
    assert row["platform"] == "LinkedIn" and row["language"] == "hi" and row["note"] == "too formal" and row["version_count"] == 2
    assert await S.feedback_col().count_documents({}) == 1


async def test_unknown_kinds_and_reasons_are_refused():
    piece = {"piece_id": "p1", "workspace_id": "w-signal", "platform": "Blog"}
    with pytest.raises(S.FeedbackError):
        await S.record_feedback(piece, kind="exploded", reason="other", user_id="u")
    with pytest.raises(S.FeedbackError):
        await S.record_feedback(piece, kind="sent_back", reason="because", user_id="u")
    assert await S.feedback_col().count_documents({}) == 0


# ---- counting ---------------------------------------------------------------------------------------------------------------------------
async def _kept(platform, language, first, final, approved=True):
    piece_id = f"s-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    await content_pieces.insert_one({"piece_id": piece_id, "workspace_id": "w-signal", "platform": platform, "language": language, "content": final,
                                     "approval_status": "approved" if approved else "pending", "deleted": False, "created_at": now})
    await content_piece_versions.insert_one({"version_id": uuid4().hex, "piece_id": piece_id, "workspace_id": "w-signal", "version_number": 1, "content": first})
    return piece_id


async def test_the_nightly_count_and_the_summary_agree():
    heavy = " ".join(f"other{i}" for i in range(40))
    await _kept("LinkedIn", "en", WORDS, WORDS)
    await _kept("LinkedIn", "en", WORDS, _one_changed())
    await _kept("LinkedIn", "en", WORDS, heavy)
    await _kept("Blog", "hi", WORDS, heavy)
    await _kept("Blog", "hi", WORDS, heavy, approved=False)  # not kept, so not counted
    piece = {"piece_id": "p-x", "workspace_id": "w-signal", "platform": "Blog", "language": "hi"}
    await S.record_feedback(piece, kind="sent_back", reason="wrong_facts", user_id="u")
    await S.record_feedback(piece, kind="regenerated", reason="wrong_facts", user_id="u")
    await S.record_feedback(piece, kind="discarded", reason="too_long", user_id="u")
    written = await S.compute_days()
    assert written >= 2
    again = await S.compute_days()
    assert again == written, "counting twice gives the same rows"
    s = await S.summary(30)
    t = s["totals"]
    assert t["untouched"] >= 1 and t["light"] >= 1 and t["heavy"] >= 2 and t["kept"] >= 4
    assert t["sent_back"] >= 1 and t["regenerated"] >= 1 and t["discarded"] >= 1
    assert s["reasons"][0]["reason"] == "wrong_facts" and s["reasons"][0]["label"] == "Wrong facts"
    assert len(s["series"]) == 30 and sum(d["heavy"] for d in s["series"]) == t["heavy"]
    blog = next(g for g in s["groups"] if g["platform"] == "Blog" and g["language"] == "hi")
    assert blog["heavy_share"] == 100.0, "every kept Blog post in Hindi was heavily edited"
    assert s["groups"][0]["platform"] == "Blog", "the weakest group is listed first"


async def test_an_empty_window_has_no_shares_not_zeros():
    s = await S.summary(7)
    assert s["untouched_share"] is None and s["heavy_share"] is None and s["groups"] == [] and len(s["series"]) == 7


# ---- the member's tap and the Ops view -------------------------------------------------------------------------------------------------
async def test_a_member_taps_a_reason_for_their_own_post(signup_user):
    owner, profile = await signup_user("Owner")
    other, other_profile = await signup_user("Other")
    ws = await create_workspace(owner, f"Signals {uuid4().hex[:6]}")
    other_ws = await create_workspace(other, f"Others {uuid4().hex[:6]}")
    mine = f"s-{uuid4().hex}"
    theirs = f"s-{uuid4().hex}"
    await content_pieces.insert_many([
        {"piece_id": mine, "workspace_id": ws, "platform": "LinkedIn", "language": "ta", "content": "x", "deleted": False},
        {"piece_id": theirs, "workspace_id": other_ws, "platform": "LinkedIn", "content": "x", "deleted": False},
    ])
    try:
        headers = {"X-Workspace-Id": ws}
        ok = await owner.post(f"/api/v1/content/pieces/{mine}/feedback", json={"kind": "regenerated", "reason": "too_long"}, headers=headers)
        assert ok.status_code == 200 and ok.json()["reason"] == "too_long"
        row = await S.feedback_col().find_one({"piece_id": mine})
        assert row["language"] == "ta" and row["workspace_id"] == ws
        assert (await owner.post(f"/api/v1/content/pieces/{mine}/feedback", json={"kind": "regenerated", "reason": "boring"}, headers=headers)).status_code == 422
        assert (await owner.post(f"/api/v1/content/pieces/{theirs}/feedback", json={"kind": "regenerated", "reason": "other"}, headers=headers)).status_code == 404
    finally:
        await content_pieces.delete_many({"piece_id": {"$in": [mine, theirs]}})


async def test_staff_with_quality_access_see_the_signals_and_customers_do_not(signup_user):
    await ST.ensure_templates()
    analyst, profile = await signup_user("Analyst")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_platform_staff": True, "staff": {"level": "staff", "role_id": "quality_analyst", "status": "active"}}})
    ST.forget()
    res = await analyst.get("/api/v1/ops/quality/signals?days=7")
    assert res.status_code == 200 and res.json()["days"] == 7
    customer, _ = await signup_user("Customer")
    assert (await customer.get("/api/v1/ops/quality/signals")).status_code == 403


def test_the_nightly_count_is_a_scheduled_job():
    from app.workers.jobs import JOBS

    assert any(j.name == "quality_signals" for j in JOBS)
