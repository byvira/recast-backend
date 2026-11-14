"""Stage 4 of sources: how a source is doing, from what became of its drafts, and Odette's weekly note."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db.mongo import get_db
from app.sources import flow, results, service
from tests.conftest import create_workspace


def test_the_verdict_says_nothing_until_there_are_enough_drafts_and_then_names_noise_mixed_and_good():
    assert results.verdict(3, 0, 0)["judgement"] == "too_early" and results.verdict(0, 0, 0)["keep_rate"] is None
    assert results.verdict(10, 1, 0)["judgement"] == "noise" and results.verdict(10, 3, 1)["judgement"] == "mixed" and results.verdict(10, 6, 4)["judgement"] == "good"
    assert results.verdict(10, 6, 4) == {"drafted": 10, "kept": 6, "published": 4, "keep_rate": 0.6, "judgement": "good"}


def test_a_piece_is_kept_when_approved_or_queued_or_published_and_published_only_when_it_went_out():
    assert results.judge({"approval_status": "pending", "publish_status": "pending"}) == (False, False)
    assert results.judge({"approval_status": "approved", "publish_status": "pending"}) == (True, False)
    assert results.judge({"approval_status": "pending", "publish_status": "queued"}) == (True, False)
    assert results.judge({"approval_status": "approved", "publish_status": "published"}) == (True, True)
    assert results.judge({"approval_status": "rejected", "publish_status": "pending"}) == (False, False)


@pytest.fixture
async def world(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Res {uuid4().hex[:6]}")
    await service.create_indexes()
    await get_db()["sources"].delete_many({})
    return client, profile["id"], ws, {"X-Workspace-Id": ws}


async def _draft(ws, source_id, n, **piece):
    """A drafted item and the piece that was made from it (found by the item's link in its text)."""
    link = f"https://example.com/post-{n}-{uuid4().hex[:4]}"
    await flow.items_col().insert_one({"workspace_id": ws, "item_id": f"i{n}-{uuid4().hex[:4]}", "source_id": source_id, "title": f"Item {n}", "link": link, "outcome": "drafted", "seen_at": datetime.now(timezone.utc)})
    if piece is not None:
        await get_db()["content_pieces"].insert_one({"piece_id": uuid4().hex, "workspace_id": ws, "content": f"Some words\n\nSource: Daily Brew ({link})", **piece})
    return link


async def test_stats_count_drafts_kept_and_published_by_finding_each_piece_by_its_link(world):
    client, user, ws, h = world
    sid = "s-" + uuid4().hex[:6]
    await _draft(ws, sid, 1, approval_status="approved", publish_status="published")
    await _draft(ws, sid, 2, approval_status="approved", publish_status="pending")
    await _draft(ws, sid, 3, approval_status="pending", publish_status="pending")
    await _draft(ws, sid, 4, approval_status="pending", publish_status="pending", deleted=True)         # removed: a draft that was not kept
    await flow.items_col().insert_one({"workspace_id": ws, "item_id": "x", "source_id": sid, "title": "no piece yet", "link": "https://example.com/writing", "outcome": "drafted", "seen_at": datetime.now(timezone.utc)})
    await flow.items_col().insert_one({"workspace_id": ws, "item_id": "y", "source_id": sid, "title": "skipped", "link": "https://example.com/off", "outcome": "skipped", "seen_at": datetime.now(timezone.utc)})
    out = await results.stats(ws, sid)
    assert out["drafted"] == 5 and out["kept"] == 2 and out["published"] == 1 and out["judgement"] == "too_early"


async def test_another_workspace_with_the_same_link_is_never_counted(world):
    client, user, ws, h = world
    sid = "s-" + uuid4().hex[:6]
    link = await _draft(ws, sid, 1, approval_status="pending")
    await get_db()["content_pieces"].insert_one({"piece_id": "other", "workspace_id": "someone-else", "content": f"Source: x ({link})", "approval_status": "approved", "publish_status": "published"})
    assert (await results.stats(ws, sid))["kept"] == 0


async def test_the_source_page_shows_the_results(world):
    client, user, ws, h = world
    brand = await get_db()["brand_profiles"].insert_one({"id": "b-" + uuid4().hex[:6], "workspace_id": ws, "user_id": user, "brand_type": "business", "identity": {"name": "Brew"}})
    brand_id = (await get_db()["brand_profiles"].find_one({"_id": brand.inserted_id}))["id"]
    sid = (await client.post("/api/v1/sources", headers=h, json={"address": "https://example.com/feed.xml", "brand_id": brand_id, "platforms": ["LinkedIn"]})).json()["id"]
    await _draft(ws, sid, 1, approval_status="approved", publish_status="pending")
    page = (await client.get(f"/api/v1/sources/{sid}", headers=h)).json()
    assert page["results"]["drafted"] == 1 and page["results"]["kept"] == 1 and page["results"]["keep_rate"] == 1.0


async def test_a_clear_result_writes_one_note_in_the_activity_log_and_not_again_within_a_week(world, monkeypatch):
    client, user, ws, h = world
    sid = "s-" + uuid4().hex[:6]
    notes: list[dict] = []

    async def record(**kw):
        notes.append(kw)

    import app.shared.activity as projector

    monkeypatch.setattr(projector, "record_system", record)
    await get_db()["sources"].insert_one({"id": sid, "workspace_id": ws, "name": "Daily Brew", "status": "active"})
    for n in range(10):
        await _draft(ws, sid, n, approval_status="pending", publish_status="pending")          # ten drafts, none kept: noise
    source = await get_db()["sources"].find_one({"id": sid})
    assert await results.advise(source) is True
    assert len(notes) == 1 and notes[0]["actor_name"] == "Odette" and notes[0]["category"] == "recommendation" and "not kept" in notes[0]["title"] and notes[0]["status"] == "warning"
    assert await results.advise(await get_db()["sources"].find_one({"id": sid})) is False and len(notes) == 1          # once a week

    good = "s-" + uuid4().hex[:6]
    await get_db()["sources"].insert_one({"id": good, "workspace_id": ws, "name": "Weekly Wins", "status": "active"})
    for n in range(10):
        await _draft(ws, good, n, approval_status="approved", publish_status="pending")
    assert await results.advise(await get_db()["sources"].find_one({"id": good})) is True and "being kept" in notes[-1]["title"] and notes[-1]["status"] == "success"

    few = "s-" + uuid4().hex[:6]
    await get_db()["sources"].insert_one({"id": few, "workspace_id": ws, "name": "New", "status": "active"})
    await _draft(ws, few, 1, approval_status="pending")
    assert await results.advise(await get_db()["sources"].find_one({"id": few})) is False                                # too early: nothing is said


async def _source_with_items(ws):
    sid = "s-" + uuid4().hex[:6]
    await get_db()["sources"].insert_one({"id": sid, "workspace_id": ws, "name": "Daily Brew", "status": "active"})
    for item_id, outcome in (("a", "drafted"), ("b", "skipped"), ("c", "drafted")):
        await flow.items_col().insert_one({"workspace_id": ws, "item_id": item_id, "source_id": sid, "title": item_id, "link": f"https://example.com/{item_id}", "outcome": outcome, "seen_at": datetime.now(timezone.utc)})
    return sid


async def test_a_tap_is_recorded_changed_and_cleared_and_shows_in_the_results(world):
    client, user, ws, h = world
    sid = await _source_with_items(ws)
    rate = lambda item, verdict: client.post(f"/api/v1/sources/{sid}/items/{item}/feedback", headers=h, json={"verdict": verdict})
    assert (await rate("a", "good")).json() == {"item_id": "a", "feedback": "good"}
    assert (await rate("b", "good")).status_code == 200                       # a good one that Ekavo had skipped
    assert (await rate("c", "not_for_me")).status_code == 200
    seen = (await client.get(f"/api/v1/sources/{sid}", headers=h)).json()
    assert seen["results"]["feedback"] == {"good": 2, "not_for_me": 1, "good_skipped": 1}
    assert {i["item_id"]: i.get("feedback") for i in seen["items"]} == {"a": "good", "b": "good", "c": "not_for_me"}
    await rate("a", "not_for_me")                                              # a tap can be changed
    assert (await rate("c", "none")).json()["feedback"] is None                # and cleared
    again = (await client.get(f"/api/v1/sources/{sid}", headers=h)).json()["results"]["feedback"]
    assert again == {"good": 1, "not_for_me": 1, "good_skipped": 1}


async def test_a_tap_never_changes_the_source_settings(world):
    client, user, ws, h = world
    sid = await _source_with_items(ws)
    before = await get_db()["sources"].find_one({"id": sid}, {"_id": 0})
    for _ in range(6):
        await client.post(f"/api/v1/sources/{sid}/items/c/feedback", headers=h, json={"verdict": "not_for_me"})
    assert await get_db()["sources"].find_one({"id": sid}, {"_id": 0}) == before


async def test_a_tap_on_an_unknown_item_or_another_workspaces_source_is_refused(world):
    client, user, ws, h = world
    sid = await _source_with_items(ws)
    assert (await client.post(f"/api/v1/sources/{sid}/items/zzz/feedback", headers=h, json={"verdict": "good"})).status_code == 404
    assert (await client.post(f"/api/v1/sources/nope/items/a/feedback", headers=h, json={"verdict": "good"})).status_code == 404
    assert (await client.post(f"/api/v1/sources/{sid}/items/a/feedback", headers=h, json={"verdict": "love"})).status_code == 422
    other = await create_workspace(client, f"Other {uuid4().hex[:6]}")
    assert (await client.post(f"/api/v1/sources/{sid}/items/a/feedback", headers={"X-Workspace-Id": other}, json={"verdict": "good"})).status_code == 404
