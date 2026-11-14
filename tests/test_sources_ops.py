"""Stage 5 of sources: the tools exist with the right shape, and Ops can switch Sources off, see usage and failures, and spot abuse."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core import staff
from app.db.mongo import get_db, users
from app.sources import flow, ops
from app.tools import ledger, undo
from app.tools.contract import Risk, Scope
from app.tools.registry import get_registry
from tests.conftest import create_workspace, signup_new_user

O = "/api/v1/ops/sources"


def test_the_ten_tools_exist_and_none_can_publish():
    reg = get_registry()
    names = {n for n in reg.names() if n.startswith("sources.")}
    assert names == {"sources.list", "sources.get", "sources.check", "sources.create", "sources.update", "sources.pause", "sources.resume", "sources.run", "sources.remove", "sources.rate_item"}
    assert all(reg.get(n).risk is not Risk.PUBLISH and reg.get(n).scope is not Scope.PUBLISH for n in names)
    assert reg.get("sources.list").risk is Risk.READ and reg.get("sources.get").risk is Risk.READ
    assert reg.get("sources.check").scope is Scope.CREATE                         # a read-only key cannot make Ekavo fetch an address
    assert reg.get("sources.remove").risk is Risk.DESTRUCTIVE


def test_every_changing_tool_says_how_to_take_it_back():
    for name in ("sources.create", "sources.update", "sources.pause", "sources.resume", "sources.run", "sources.check", "sources.remove"):
        assert undo.rule_for(name) is not None, name
    assert undo.can_undo("sources.create") and undo.can_undo("sources.pause") and undo.can_undo("sources.resume")


async def _staff(make_client, *, level="admin"):
    client = make_client()
    user = await signup_new_user(client, name="Staff")
    await users.update_one({"id": user["id"]}, {"$set": {"is_platform_staff": True, "is_master_admin": True, "staff": {"level": level}}})
    staff.forget(user["id"])
    return client


@pytest.fixture
async def world(signup_user, make_client):
    member, _ = await signup_user()
    ws = await create_workspace(member, f"Src {uuid4().hex[:6]}")
    await ledger.create_indexes()
    await ledger.settings().delete_many({})
    await get_db()["sources"].delete_many({})
    return member, ws, await _staff(make_client)


async def test_only_platform_staff_can_see_or_change_sources_here(world, make_client):
    member, ws, admin = world
    assert (await member.get(O)).status_code in (401, 403)
    assert (await admin.get(O)).status_code == 200
    assert (await member.put(f"{O}/switch", json={"off": True})).status_code in (401, 403)


async def test_one_switch_turns_every_sources_tool_off_and_back_on_and_is_audited(world):
    member, ws, admin = world
    assert (await admin.get(O)).json()["switch"]["off"] is False
    res = await admin.put(f"{O}/switch", json={"off": True, "message": "Sources is paused for now.", "reason": "test"})
    assert res.status_code == 200 and res.json()["off"] is True
    from app.tools.services import LiveServices

    assert await LiveServices().switched_off(ws, "sources.run") == "Sources is paused for now."
    assert await LiveServices().switched_off(ws, "sources.create") == "Sources is paused for now."
    await admin.put(f"{O}/switch", json={"off": False, "reason": "done"})
    assert await LiveServices().switched_off(ws, "sources.run") is None and (await admin.get(O)).json()["switch"]["off"] is False


async def test_run_now_is_refused_with_the_message_while_the_switch_is_off(world):
    member, ws, admin = world
    h = {"X-Workspace-Id": ws}
    brand_id = "b-" + uuid4().hex[:6]
    await get_db()["brand_profiles"].insert_one({"id": brand_id, "workspace_id": ws, "user_id": "u", "brand_type": "business", "identity": {"name": "Brew"}})
    made = await member.post("/api/v1/sources", headers=h, json={"address": "https://example.com/feed.xml", "brand_id": brand_id, "platforms": ["LinkedIn"]})
    assert made.status_code == 201, made.text
    await admin.put(f"{O}/switch", json={"off": True, "message": "Sources is paused for now."})
    res = await member.post(f"/api/v1/sources/{made.json()['id']}/run", headers=h)
    assert res.status_code == 503 and "paused for now" in res.json()["detail"]


async def test_failures_lists_sources_that_stopped_themselves_with_the_reason(world):
    member, ws, admin = world
    await get_db()["sources"].insert_many([
        {"id": "p1", "workspace_id": ws, "name": "Broken", "address": "https://x.test/f", "status": "paused", "paused_reason": "The address stopped working.", "health": {"fail_streak": 3, "last_error": "timeout"}},
        {"id": "ok", "workspace_id": ws, "name": "Fine", "address": "https://y.test/f", "status": "active", "health": {"fail_streak": 0}},
        {"id": "w1", "workspace_id": ws, "name": "Wobbly", "address": "https://z.test/f", "status": "active", "health": {"fail_streak": 1}},
    ])
    rows = (await admin.get(O)).json()["failures"]
    assert {r["id"] for r in rows} == {"p1", "w1"} and next(r for r in rows if r["id"] == "p1")["reason"] == "The address stopped working."


async def test_usage_counts_sources_by_status_and_items_by_outcome(world):
    member, ws, admin = world
    await get_db()["sources"].insert_many([{"id": "a", "workspace_id": ws, "status": "active"}, {"id": "b", "workspace_id": ws, "status": "paused"}])
    now = datetime.now(timezone.utc)
    await flow.items_col().insert_many([{"workspace_id": ws, "item_id": f"i{n}", "source_id": "a", "outcome": "drafted", "seen_at": now} for n in range(3)]
                                       + [{"workspace_id": ws, "item_id": "s", "source_id": "a", "outcome": "skipped", "seen_at": now}])
    usage = (await admin.get(O)).json()["usage"]
    assert usage["sources_by_status"]["active"] >= 1 and usage["items_by_outcome"]["drafted"] >= 3 and usage["items_by_outcome"]["skipped"] >= 1
    assert any(w["workspace_id"] == ws and w["drafted"] >= 3 for w in usage["top_workspaces"])


async def test_the_abuse_watch_flags_many_new_sources_many_refusals_and_many_stopped_sources(world):
    member, ws, admin = world
    now = datetime.now(timezone.utc)
    await get_db()["sources"].insert_many([{"id": f"n{i}", "workspace_id": ws, "status": "active", "created_at": now} for i in range(ops.NEW_SOURCES_PER_DAY)])
    other = "ws-" + uuid4().hex[:6]
    await ledger.calls().insert_many([{"workspace_id": other, "tool": "sources.check", "outcome": "refused", "code": "scope_missing", "at": now} for _ in range(ops.REFUSED_PER_HOUR)])
    third = "ws-" + uuid4().hex[:6]
    await get_db()["sources"].insert_many([{"id": f"x{i}", "workspace_id": third, "status": "paused", "paused_reason": "stopped"} for i in range(ops.PAUSED_SOURCES)])
    flagged = {r["workspace_id"]: [x["why"] for x in r["reasons"]] for r in (await admin.get(O)).json()["abuse"]}
    assert flagged[ws] == ["Added many sources in a day"] and flagged[other] == ["Many refused source calls in an hour"] and flagged[third] == ["Many sources stopped themselves"]
    calm = "ws-" + uuid4().hex[:6]
    await get_db()["sources"].insert_one({"id": "one", "workspace_id": calm, "status": "active", "created_at": now})
    assert calm not in {r["workspace_id"] for r in (await admin.get(O)).json()["abuse"]}
