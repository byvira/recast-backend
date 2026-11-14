"""Stage 3 of sources: making one, checking it, the limits, health and pausing, and the scheduled pass. The network, the tools and the relevance scorer are stand-ins."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.db.mongo import get_db
from app.sources import flow, service
from app.sources.fetch import FetchError, Fetched
from tests.conftest import create_workspace

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Daily Brew</title>
<item><title>Fresh roast</title><link>https://example.com/roast</link><description>A new roast is here</description></item>
<item><title>Brewing tips</title><link>https://example.com/tips</link><description>Three tips for better coffee</description></item></channel></rss>"""

PAGE = b"<html><head><title>Our launch</title></head><body><article><p>" + b"We are launching a new product this month and it is a big deal for everyone. " * 6 + b"</p></article></body></html>"


def _fetcher(body=RSS, ctype="application/rss+xml", fail=None):
    async def fetcher(url):
        if fail:
            raise fail
        return Fetched(url, ctype, body)

    return fetcher


class Tools:
    def __init__(self):
        self.calls = []

    async def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        return {"data": {"piece_id": "p"}, "run": None}


async def _everything(brand, item):
    return 0.9


@pytest.fixture
async def world(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Src {uuid4().hex[:6]}")
    await service.create_indexes()
    await get_db()["sources"].delete_many({})            # the scheduled pass looks at every workspace, so each test starts with no sources from another
    brand = await get_db()["brand_profiles"].insert_one({"id": "b-" + uuid4().hex[:6], "workspace_id": ws, "user_id": profile["id"], "brand_type": "business", "identity": {"name": "Brew Co", "description": "coffee"}})
    brand_id = (await get_db()["brand_profiles"].find_one({"_id": brand.inserted_id}))["id"]
    return client, profile["id"], ws, brand_id, {"X-Workspace-Id": ws}


def _body(brand, **over):
    return {"address": "https://example.com/feed.xml", "brand_id": brand, "platforms": ["LinkedIn"], "kind": "feed", "relevance": "everything", **over}


async def test_a_source_is_made_listed_changed_paused_resumed_and_removed(world):
    client, user, ws, brand, h = world
    made = await client.post("/api/v1/sources", headers=h, json=_body(brand, name="Daily Brew"))
    assert made.status_code == 201, made.text
    sid = made.json()["id"]
    assert made.json()["status"] == "active" and made.json()["check_every_hours"] == 6 and made.json()["mode"] == "link"
    assert [s["id"] for s in (await client.get("/api/v1/sources", headers=h)).json()["items"]] == [sid]
    changed = (await client.patch(f"/api/v1/sources/{sid}", headers=h, json={"mode": "write", "daily_limit": 3, "relevance": "close"})).json()
    assert changed["mode"] == "write" and changed["daily_limit"] == 3 and changed["relevance"] == "close"
    assert (await client.post(f"/api/v1/sources/{sid}/pause", headers=h)).json()["status"] == "paused"
    assert (await client.post(f"/api/v1/sources/{sid}/resume", headers=h)).json()["status"] == "active"
    assert (await client.delete(f"/api/v1/sources/{sid}", headers=h)).status_code == 200
    assert (await client.get("/api/v1/sources", headers=h)).json()["items"] == [] and (await client.get(f"/api/v1/sources/{sid}", headers=h)).status_code == 404


async def test_a_bad_address_brand_platform_or_setting_is_refused_with_a_plain_reason(world):
    client, user, ws, brand, h = world
    for over, code in (({"address": "http://example.com/feed"}, 422), ({"address": "https://localhost/feed"}, 422), ({"address": "https://user:pw@example.com/f"}, 422),
                       ({"brand_id": "someone-elses"}, 422), ({"platforms": []}, 422), ({"mode": "publish"}, 422), ({"check_every_hours": 1}, 422), ({"daily_limit": 500}, 422)):
        res = await client.post("/api/v1/sources", headers=h, json=_body(brand, **over))
        assert res.status_code == code, (over, res.text)
    assert (await client.post("/api/v1/sources", headers=h, json=_body(brand))).status_code == 201
    dup = await client.post("/api/v1/sources", headers=h, json=_body(brand))
    assert dup.status_code == 409 and "already follow" in dup.json()["detail"]


async def test_a_workspace_can_follow_only_twenty_sources(world):
    client, user, ws, brand, h = world
    for n in range(service.MAX_SOURCES):                      # made through the service: the route allows 20 a minute, which is its own limit
        await service.create(ws, user, _body(brand, address=f"https://example.com/feed{n}"))
    full = await client.post("/api/v1/sources", headers=h, json=_body(brand, address="https://example.com/one-more"))
    assert full.status_code == 409 and str(service.MAX_SOURCES) in full.json()["detail"]


async def test_a_source_belongs_to_its_workspace(world, signup_user):
    client, user, ws, brand, h = world
    sid = (await client.post("/api/v1/sources", headers=h, json=_body(brand))).json()["id"]
    other, _ = await signup_user()
    other_ws = await create_workspace(other, f"Other {uuid4().hex[:6]}")
    assert (await other.get(f"/api/v1/sources/{sid}", headers={"X-Workspace-Id": other_ws})).status_code == 404
    assert (await other.post(f"/api/v1/sources/{sid}/pause", headers={"X-Workspace-Id": other_ws})).status_code == 404


async def test_checking_an_address_shows_its_first_items_and_makes_nothing(world, monkeypatch):
    client, user, ws, brand, h = world

    async def fake(url, **kw):
        return Fetched(url, "application/rss+xml", RSS)

    monkeypatch.setattr(service, "fetch", fake)
    shown = (await client.post("/api/v1/sources/check", headers=h, json={"address": "https://example.com/feed.xml"})).json()
    assert shown["count"] == 2 and shown["items"][0]["title"] == "Fresh roast" and "new roast" in shown["items"][0]["summary"]
    assert await get_db()["sources"].count_documents({"workspace_id": ws}) == 0 and await flow.items_col().count_documents({"workspace_id": ws}) == 0

    async def refuse(url, **kw):
        raise FetchError("needs_login", "That address needs a login.")

    monkeypatch.setattr(service, "fetch", refuse)
    refused = await client.post("/api/v1/sources/check", headers=h, json={"address": "https://example.com/feed.xml"})
    assert refused.status_code == 422 and "login" in refused.json()["detail"]


async def test_a_check_drafts_new_items_records_the_run_and_a_second_check_makes_nothing_new(world):
    client, user, ws, brand, h = world
    made = (await client.post("/api/v1/sources", headers=h, json=_body(brand, name="Daily Brew", include_older=True))).json()
    source = await service._load(ws, made["id"])
    tools = Tools()
    first = await service.run(source, tools=tools, score=_everything, fetcher=_fetcher())
    assert first["ok"] and first["drafted"] == 2 and [c[0] for c in tools.calls] == ["library.create_draft"] * 2
    after = (await client.get(f"/api/v1/sources/{made['id']}", headers=h)).json()
    assert after["source"]["counts"]["drafted"] == 2 and after["source"]["health"]["fail_streak"] == 0 and after["source"]["last_run_at"] and len(after["items"]) == 2
    again = await service.run(await service._load(ws, made["id"]), tools=Tools(), score=_everything, fetcher=_fetcher())
    assert again["drafted"] == 0 and again["skipped"] == {"duplicate": 2}


async def test_a_page_source_drafts_the_page_once_and_a_changed_page_is_a_new_item(world):
    client, user, ws, brand, h = world
    made = (await client.post("/api/v1/sources", headers=h, json=_body(brand, address="https://example.com/launch", kind="page"))).json()
    source = await service._load(ws, made["id"])
    one = await service.run(source, tools=Tools(), score=_everything, fetcher=_fetcher(PAGE, "text/html"))
    assert one["ok"] and one["drafted"] == 1
    same = await service.run(await service._load(ws, made["id"]), tools=Tools(), score=_everything, fetcher=_fetcher(PAGE, "text/html"))
    assert same["drafted"] == 0
    changed = PAGE.replace(b"launching", b"shipping")
    new = await service.run(await service._load(ws, made["id"]), tools=Tools(), score=_everything, fetcher=_fetcher(changed, "text/html"))
    assert new["drafted"] == 1


async def test_a_feed_address_that_is_not_a_feed_is_a_failure_with_the_plain_words(world):
    client, user, ws, brand, h = world
    made = (await client.post("/api/v1/sources", headers=h, json=_body(brand))).json()
    out = await service.run(await service._load(ws, made["id"]), tools=Tools(), score=_everything, fetcher=_fetcher(b"<html><body>Hello</body></html>", "text/html"))
    assert out["ok"] is False and out["code"] == "not_a_feed" and "feed" in out["message"]
    row = await service._load(ws, made["id"])
    assert row["status"] == "paused" and "feed" in row["paused_reason"]                    # a wrong kind of address will not get better by waiting: it pauses at once


async def test_three_busy_answers_in_a_row_pause_a_source_with_a_reason_and_a_good_check_resets_the_count(world):
    client, user, ws, brand, h = world
    made = (await client.post("/api/v1/sources", headers=h, json=_body(brand, include_older=True))).json()
    busy = FetchError("site_busy", "That site is busy or down right now.", True)
    for n in (1, 2):
        out = await service.run(await service._load(ws, made["id"]), tools=Tools(), score=_everything, fetcher=_fetcher(fail=busy))
        row = await service._load(ws, made["id"])
        assert out["ok"] is False and row["status"] == "active" and row["health"]["fail_streak"] == n
    await service.run(await service._load(ws, made["id"]), tools=Tools(), score=_everything, fetcher=_fetcher())
    assert (await service._load(ws, made["id"]))["health"]["fail_streak"] == 0
    for _ in range(service.FAILS_BEFORE_PAUSE):
        await service.run(await service._load(ws, made["id"]), tools=Tools(), score=_everything, fetcher=_fetcher(fail=busy))
    row = await service._load(ws, made["id"])
    assert row["status"] == "paused" and "busy" in row["paused_reason"]
    assert (await client.post(f"/api/v1/sources/{made['id']}/resume", headers=h)).json()["health"]["fail_streak"] == 0


async def test_the_scheduled_pass_runs_only_due_active_sources_and_pauses_one_whose_owner_left(world, monkeypatch):
    client, user, ws, brand, h = world
    due = (await client.post("/api/v1/sources", headers=h, json=_body(brand, address="https://example.com/a"))).json()["id"]
    later = (await client.post("/api/v1/sources", headers=h, json=_body(brand, address="https://example.com/b"))).json()["id"]
    paused = (await client.post("/api/v1/sources", headers=h, json=_body(brand, address="https://example.com/c"))).json()["id"]
    await get_db()["sources"].update_one({"id": later}, {"$set": {"next_check_at": datetime.now(timezone.utc) + timedelta(hours=3)}})
    await client.post(f"/api/v1/sources/{paused}/pause", headers=h)
    ran: list[str] = []

    async def fake_run(source, **kw):
        ran.append(source["id"])
        return {"ok": True}

    monkeypatch.setattr(service, "run", fake_run)
    assert await service.run_due() == 1 and ran == [due]

    await get_db()["sources"].update_one({"id": due}, {"$set": {"next_check_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    await get_db()["workspace_members"].update_one({"workspace_id": ws, "user_id": user}, {"$set": {"status": "removed"}})
    ran.clear()
    assert await service.run_due() == 0 and ran == []
    row = await service._load(ws, due)
    assert row["status"] == "paused" and "no longer in the workspace" in row["paused_reason"]


async def test_a_workspace_switch_holds_a_source_where_it_is_and_nothing_runs(world, monkeypatch):
    client, user, ws, brand, h = world
    sid = (await client.post("/api/v1/sources", headers=h, json=_body(brand))).json()["id"]
    from app.tools.services import LiveServices

    async def off(self, workspace_id, tool):
        return "This workspace has switched that off for connected apps."

    monkeypatch.setattr(LiveServices, "switched_off", off)
    ran: list[str] = []

    async def fake_run(source, **kw):
        ran.append(source["id"])

    monkeypatch.setattr(service, "run", fake_run)
    assert await service.run_due() == 0 and ran == []
    assert (await service._load(ws, sid))["status"] == "active"                          # held, not paused: it runs once the switch is released


async def test_write_mode_at_zero_credits_waits_and_does_not_check(world, monkeypatch):
    client, user, ws, brand, h = world
    sid = (await client.post("/api/v1/sources", headers=h, json=_body(brand, mode="write"))).json()["id"]
    from app.billing import credits

    async def none_left(workspace_id, n):
        return False

    notices: list[str] = []

    async def notice(workspace_id, name, **kw):
        notices.append(name)

    monkeypatch.setattr(credits, "has_room", none_left)
    monkeypatch.setattr(credits, "pause_notice", notice)
    ran: list[str] = []

    async def fake_run(source, **kw):
        ran.append(source["id"])

    monkeypatch.setattr(service, "run", fake_run)
    assert await service.run_due() == 0 and ran == [] and len(notices) == 1 and (await service._load(ws, sid))["status"] == "active"


async def test_only_the_person_who_set_a_source_up_can_run_it_now(world, signup_user):
    client, user, ws, brand, h = world
    sid = (await client.post("/api/v1/sources", headers=h, json=_body(brand))).json()["id"]
    await get_db()["sources"].update_one({"id": sid}, {"$set": {"created_by": "someone-else"}})
    res = await client.post(f"/api/v1/sources/{sid}/run", headers=h)
    assert res.status_code == 403 and "works as them" in res.json()["detail"]
