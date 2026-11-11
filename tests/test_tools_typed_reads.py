"""The typed read tools (who am I, credits, prices, plan limits, posts, what is waiting, what is scheduled, campaigns, brands, runs, top assets),
called through the public API. Each answer is held to the tool's own output type, and lists page with a cursor that survives new rows."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.mongo import get_db
from app.tools import ledger
from tests.conftest import create_workspace

D = "/api/v1/developer"
P = "/api/public/v1"


@pytest.fixture
async def world(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Reads {uuid4().hex[:6]}")
    await ledger.create_indexes()
    await ledger.settings().delete_many({})
    made = await client.post(f"{D}/tool-keys", headers={"X-Workspace-Id": ws}, json={"name": "Reader", "scopes": ["read"]})
    assert made.status_code == 201, made.text
    return client, ws, {"Authorization": f"Bearer {made.json()['secret']}"}


async def _read(client, h, tool, **arguments):
    res = await client.post(f"{P}/tools/{tool}/call", headers=h, json={"arguments": arguments})
    assert res.status_code == 200, res.text
    return res.json()["data"]


async def _pieces(ws: str, count: int, **over) -> list[str]:
    base = datetime.now(timezone.utc) - timedelta(days=1)
    ids = []
    for i in range(count):
        pid = uuid4().hex
        ids.append(pid)
        await get_db()["content_pieces"].insert_one({
            "piece_id": pid, "id": pid, "workspace_id": ws, "platform": "LinkedIn", "content": f"Post number {i} about launch", "approval_status": "approved",
            "publish_status": "pending", "created_at": base + timedelta(minutes=i), **over,
        })
    return ids


async def test_the_account_reads_answer_in_their_typed_shape(world):
    client, ws, h = world
    who = await _read(client, h, "me.whoami")
    assert who["workspace_id"] == ws and who["role"] == "owner" and who["plan"]
    credits = await _read(client, h, "credits.summary")
    assert credits["used"] == 0 and credits["period"] and credits["plan_name"]
    prices = await _read(client, h, "credits.prices")
    assert {"text_post", "narration"} <= {p["key"] for p in prices["prices"]} and all(p["kind"] in ("active", "passive") for p in prices["prices"])
    limits = await _read(client, h, "plan.limits")
    assert limits["plan"] and any(row["key"] == "pictures_day" for row in limits["limits"])


async def test_posts_page_newest_first_and_a_new_post_never_repeats_or_hides_another(world):
    client, ws, h = world
    ids = await _pieces(ws, 5)
    first = await _read(client, h, "library.posts", limit=2)
    assert [r["id"] for r in first["items"]] == [ids[4], ids[3]] and first["next_cursor"]
    assert first["items"][0]["excerpt"].startswith("Post number 4") and first["items"][0]["status"] in ("drafting", "staging", "scheduled", "published", "failed", "archived")

    newest = (await _pieces(ws, 1, created_at=datetime.now(timezone.utc)))[0]          # made between the two pages
    second = await _read(client, h, "library.posts", limit=2, cursor=first["next_cursor"])
    third = await _read(client, h, "library.posts", limit=2, cursor=second["next_cursor"])
    seen = [r["id"] for r in first["items"] + second["items"] + third["items"]]
    assert seen == [ids[4], ids[3], ids[2], ids[1], ids[0]] and newest not in seen and third["next_cursor"] is None


async def test_a_bad_cursor_is_refused_and_a_search_finds_words(world):
    client, ws, h = world
    await _pieces(ws, 2)
    bad = await client.post(f"{P}/tools/library.posts/call", headers=h, json={"arguments": {"cursor": "not-a-cursor"}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "input_invalid"
    found = await _read(client, h, "library.posts", q="number 1")
    assert [r["excerpt"][:13] for r in found["items"]] == ["Post number 1"]


async def test_one_post_in_full_and_a_missing_one_in_plain_words(world):
    client, ws, h = world
    pid = (await _pieces(ws, 1))[0]
    post = await _read(client, h, "library.post", id=pid)
    assert post["id"] == pid and post["content"].startswith("Post number 0") and post["approval_status"] == "approved"
    missing = await client.post(f"{P}/tools/library.post/call", headers=h, json={"arguments": {"id": "nope"}})
    assert missing.status_code == 404 and "not found" in missing.json()["error"]["message"]


async def test_waiting_for_approval_lists_only_pending_posts_with_the_reason_a_post_was_held(world):
    client, ws, h = world
    await _pieces(ws, 2)
    pending = await _pieces(ws, 1, approval_status="pending", flagged_for_review=True, safety_reason={"message": "Mentions a health claim"})
    waiting = await _read(client, h, "approvals.waiting")
    assert [r["id"] for r in waiting["items"]] == pending and waiting["items"][0]["needs_review_reason"] == "Mentions a health claim"


async def test_the_calendar_lists_what_is_queued_soonest_first_between_two_dates_and_pages_by_time(world):
    client, ws, h = world
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    ids = []
    for i in (3, 1, 2):
        ids += await _pieces(ws, 1, publish_status="queued", publish_scheduled_at=soon + timedelta(hours=i))
    await _pieces(ws, 1, publish_status="queued", publish_scheduled_at=soon + timedelta(days=60))       # outside the window
    await _pieces(ws, 1, publish_status="published", publish_scheduled_at=soon)                         # already out
    page = await _read(client, h, "calendar.upcoming", limit=2)
    assert [r["id"] for r in page["items"]] == [ids[1], ids[2]] and page["next_cursor"]
    rest = await _read(client, h, "calendar.upcoming", limit=2, cursor=page["next_cursor"])
    assert [r["id"] for r in rest["items"]] == [ids[0]] and rest["next_cursor"] is None
    bad = await client.post(f"{P}/tools/calendar.upcoming/call", headers=h, json={"arguments": {"start": "2026-12-31", "end": "2026-01-01"}})
    assert bad.status_code == 422


async def test_campaigns_and_brands_list_in_their_typed_shape(world):
    client, ws, h = world
    now = datetime.now(timezone.utc)
    await get_db()["campaigns"].insert_one({"id": uuid4().hex, "workspace_id": ws, "name": "Launch week", "status": "active", "platforms": ["LinkedIn"], "created_at": now, "updated_at": now})
    await get_db()["brand_profiles"].insert_one({"id": uuid4().hex, "workspace_id": ws, "brand_type": "Business", "identity": {"name": "Acme"}, "is_default": True, "created_at": now})
    campaigns = await _read(client, h, "campaigns.list")
    assert campaigns["items"][0]["name"] == "Launch week" and campaigns["items"][0]["platforms"] == ["LinkedIn"]
    assert (await _read(client, h, "campaigns.list", status="paused"))["items"] == []
    brands = await _read(client, h, "brands.list")
    assert brands["brands"][0]["name"] == "Acme" and brands["brands"][0]["is_default"] is True


async def test_runs_and_top_assets_answer_when_there_is_nothing_yet(world):
    client, ws, h = world
    assert (await _read(client, h, "runs.list"))["items"] == []
    assert (await _read(client, h, "analytics.top_assets", kind="image"))["items"] == []


async def test_a_workspace_never_sees_another_workspaces_posts(world, signup_user):
    client, ws, h = world
    other = await create_workspace(client, f"Other {uuid4().hex[:6]}")
    await _pieces(other, 3)
    assert (await _read(client, h, "library.posts"))["items"] == []
