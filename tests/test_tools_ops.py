"""The Ops platform layer: who may see and change it, usage, every call, the three switches (all tools, one tool, one workspace), every key,
the abuse watch, and the audit of each change. The switches are proven against the real public API, so a switch is shown to act on the very
next call."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core import staff
from app.db.mongo import users
from app.tools import ledger, ops
from tests.conftest import create_workspace, signup_new_user

O = "/api/v1/ops/tools"
D = "/api/v1/developer"
P = "/api/public/v1"


async def _staff(make_client, *, master=False, level="admin"):
    client = make_client()
    user = await signup_new_user(client, name="Staff")
    fields = {"is_platform_staff": True, "staff": {"level": level}}
    if master:
        fields["is_master_admin"] = True
    await users.update_one({"id": user["id"]}, {"$set": fields})
    staff.forget(user["id"])
    return client, user


@pytest.fixture
async def world(signup_user, make_client):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Ops {uuid4().hex[:6]}")
    await ledger.create_indexes()
    await ops.create_indexes()
    await ledger.settings().delete_many({})
    h = {"X-Workspace-Id": ws}
    made = await client.post(f"{D}/tool-keys", headers=h, json={"name": "Zapier", "scopes": ["read"]})
    assert made.status_code == 201, made.text
    key = {"Authorization": f"Bearer {made.json()['secret']}"}
    admin, _ = await _staff(make_client, master=True)
    return client, ws, h, key, made.json()["key"]["id"], admin


async def _call(client, key, tool="plan.limits"):
    return await client.post(f"{P}/tools/{tool}/call", headers=key, json={"arguments": {}})


async def test_only_staff_with_the_platform_area_can_see_or_change_anything_here(world, make_client):
    client, ws, h, key, key_id, admin = world
    assert (await client.get(f"{O}/overview")).status_code in (401, 403)                 # an ordinary member
    limited, _ = await _staff(make_client, level="staff")
    assert (await limited.get(f"{O}/overview")).status_code == 403                       # staff with no grant on this area
    assert (await admin.get(f"{O}/overview")).status_code == 200
    assert "platform" in staff.AREAS


async def test_the_overview_counts_calls_refusals_tools_surfaces_and_keys(world):
    client, ws, h, key, key_id, admin = world
    for _ in range(3):
        assert (await _call(client, key)).status_code == 200
    assert (await client.post(f"{P}/tools/presets.create/call", headers=key, json={"arguments": {}})).status_code in (403, 422)    # refused
    data = (await admin.get(f"{O}/overview")).json()
    assert data["calls"] >= 4 and data["by_outcome"].get("done", 0) >= 3 and data["tools_in_catalog"] >= 400
    assert data["surfaces"].get("api", 0) >= 4 and data["keys"]["active"] >= 1
    assert any(t["tool"] == "plan.limits" and t["calls"] >= 3 for t in data["top_tools"])
    assert data["refusals"]


async def test_every_call_can_be_listed_and_filtered_and_pages_by_time(world):
    client, ws, h, key, key_id, admin = world
    for _ in range(3):
        await _call(client, key)
    page = (await admin.get(f"{O}/calls", params={"workspace_id": ws, "tool": "plan.limits", "limit": 2})).json()
    assert len(page["items"]) == 2 and page["next_before"] and all(r["tool"] == "plan.limits" and r["workspace_id"] == ws for r in page["items"])
    rest = (await admin.get(f"{O}/calls", params={"workspace_id": ws, "tool": "plan.limits", "limit": 2, "before": page["next_before"]})).json()
    assert len(rest["items"]) >= 1 and rest["items"][0]["id"] not in {r["id"] for r in page["items"]}
    assert (await admin.get(f"{O}/calls", params={"credential_id": key_id})).json()["items"]
    row = page["items"][0]
    assert "arguments" not in row and row["fields"] == []                                  # field names only, never what they held


async def test_the_tool_table_shows_each_tool_with_how_it_is_undone_and_how_it_is_used(world):
    client, ws, h, key, key_id, admin = world
    await _call(client, key)
    table = {t["name"]: t for t in (await admin.get(f"{O}/tools")).json()["tools"]}
    assert len(table) >= 400 and table["plan.limits"]["calls"] >= 1 and table["video.trim_clip"]["undo"] == "version" and table["publish.now"]["undo"] == "final"
    assert all(t["wired"] for t in table.values())


async def test_switching_one_tool_off_acts_on_the_next_call_says_why_and_is_audited(world):
    client, ws, h, key, key_id, admin = world
    assert (await _call(client, key)).status_code == 200
    off = await admin.put(f"{O}/switches/tools/plan.limits", json={"off": True, "message": "Paused while we fix it.", "reason": "bug 123"})
    assert off.status_code == 200 and off.json() == {"tool": "plan.limits", "off": True}
    blocked = await _call(client, key)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "tool_off" and blocked.json()["error"]["message"] == "Paused while we fix it."
    assert (await _call(client, key, "credits.summary")).status_code == 200                # other tools are not touched
    assert (await admin.put(f"{O}/switches/tools/plan.limits", json={"off": False})).status_code == 200
    assert (await _call(client, key)).status_code == 200
    assert (await admin.put(f"{O}/switches/tools/nothing.here", json={"off": True})).status_code == 404

    trail = (await admin.get(f"{O}/audit")).json()["items"]
    first = next(r for r in trail if r["action"] == "tools.switch_tool" and r["reason"] == "bug 123")
    assert first["target"] == "plan.limits" and first["before"] is False and first["after"] is True and first["by_email"]


async def test_switching_everything_off_is_for_the_master_admin_only_and_stops_every_key(world, make_client):
    client, ws, h, key, key_id, admin = world
    plain, _ = await _staff(make_client, level="admin")
    assert (await plain.put(f"{O}/switches/global", json={"off": True})).status_code == 403
    assert (await admin.put(f"{O}/switches/global", json={"off": True, "message": "Down for a short while."})).status_code == 200
    stopped = await _call(client, key)
    assert stopped.status_code == 403 and stopped.json()["error"]["message"] == "Down for a short while."
    assert (await admin.get(f"{O}/switches")).json()["global"]["off"] is True
    assert (await admin.put(f"{O}/switches/global", json={"off": False})).status_code == 200
    assert (await _call(client, key)).status_code == 200


async def test_a_workspace_can_be_switched_off_by_ops_or_by_its_own_owner(world):
    client, ws, h, key, key_id, admin = world
    assert (await admin.put(f"{O}/switches/workspaces/{ws}", json={"all_off": True, "reason": "abuse report"})).status_code == 200
    assert (await _call(client, key)).json()["error"]["code"] == "tool_off"
    assert (await admin.put(f"{O}/switches/workspaces/{ws}", json={"all_off": False, "off_tools": ["plan.limits"]})).status_code == 200
    assert (await _call(client, key)).status_code == 403 and (await _call(client, key, "credits.summary")).status_code == 200
    assert (await admin.put(f"{O}/switches/workspaces/{ws}", json={"all_off": False, "off_tools": ["nope.nope"]})).status_code == 404

    own = await client.put(f"{D}/tool-switches", headers=h, json={"all_off": False, "off_tools": []})        # the owner switches it back on
    assert own.status_code == 200 and (await _call(client, key)).status_code == 200
    seen = (await client.get(f"{D}/tool-switches", headers=h)).json()
    assert seen == {"all_off": False, "off_tools": [], "everything_off_by_ekavo": False}


async def test_ops_sees_every_key_and_can_revoke_one_at_once_with_a_reason_on_record(world):
    client, ws, h, key, key_id, admin = world
    await _call(client, key)
    keys = (await admin.get(f"{O}/keys", params={"workspace_id": ws})).json()["keys"]
    assert [k["id"] for k in keys] == [key_id] and keys[0]["calls_7d"] >= 1 and "hash" not in keys[0] and "secret" not in keys[0]
    assert (await admin.post(f"{O}/keys/{key_id}/revoke", json={"reason": "leaked on a forum"})).status_code == 200
    assert (await _call(client, key)).status_code == 401                                        # the next call, not the next hour
    assert (await admin.post(f"{O}/keys/{key_id}/revoke", json={})).status_code == 404
    assert any(r["action"] == "tools.revoke_key" and r["reason"] == "leaked on a forum" for r in (await admin.get(f"{O}/audit")).json()["items"])


async def test_the_abuse_watch_flags_a_key_that_is_mostly_refused_and_someone_guessing_keys(world):
    client, ws, h, key, key_id, admin = world
    now = datetime.now(timezone.utc)
    for i in range(30):
        await ledger.record({"credential_id": "k-bad", "workspace_id": ws, "tool": "presets.create", "outcome": "refused" if i < 20 else "done", "code": "scope_missing", "at": now})
    for i in range(12):
        await ledger.record({"credential_id": "k-limited", "workspace_id": ws, "tool": "plan.limits", "outcome": "refused", "code": "rate_limited", "at": now})
    for _ in range(25):
        await ops.record_auth_failure("203.0.113.9")
    found = (await admin.get(f"{O}/abuse")).json()["findings"]
    kinds = {(f["kind"], f.get("credential_id") or f.get("who")) for f in found}
    assert ("mostly_refused", "k-bad") in kinds and ("rate_limited", "k-limited") in kinds and ("guessing_keys", ops.who_hash("203.0.113.9")) in kinds
    assert "203.0.113.9" not in str(found)                                                      # only a short hash of the address is ever kept


async def test_a_call_with_a_key_that_does_not_exist_is_counted_for_the_watch(world):
    client, ws, h, key, key_id, admin = world
    before = await ops.failures().count_documents({})
    res = await client.post(f"{P}/tools/plan.limits/call", headers={"Authorization": "Bearer ek_live_nope"}, json={"arguments": {}})
    assert res.status_code == 401
    import asyncio

    await asyncio.sleep(0.2)
    assert await ops.failures().count_documents({}) == before + 1


async def test_a_quiet_workspace_shows_nothing_in_the_watch(world):
    client, ws, h, key, key_id, admin = world
    await ledger.calls().delete_many({})                                                          # the database is shared by every test
    await _call(client, key)
    await ops.failures().delete_many({})
    assert (await admin.get(f"{O}/abuse")).json()["findings"] == []
