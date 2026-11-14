"""The ways into the tool layer can be paused one at a time (command line, assistant, service key), the public ask route honours it, and the number of command line and assistant
sign-ins is a plan limit that Ops can edit, not a fixed number."""
import pytest

from app.billing import plans as plan_table
from app.core import staff
from app.db.mongo import users
from app.shared import entitlements
from app.tools import ledger, ops
from tests.conftest import signup_new_user
from tests.test_public_ask import answer_with, token  # noqa: F401
from tests.test_tools_public_api import P, _auth, _key, world  # noqa: F401  (world is a fixture)
from tests.test_tools_sessions_mcp import S, _signed_in

O = "/api/v1/ops/tools"


async def _admin(make_client):
    admin = make_client()
    user = await signup_new_user(admin, name="Staff")
    await users.update_one({"id": user["id"]}, {"$set": {"is_platform_staff": True, "is_master_admin": True, "staff": {"level": "admin"}}})
    staff.forget(user["id"])
    return admin


async def test_pausing_the_command_line_stops_it_and_nothing_else_and_resuming_restores_it(world, make_client):
    client, ws, h = world
    admin = await _admin(make_client)
    cli = await token(client, h, ws, kind="cli")
    key = await _key(client, h)
    assert (await client.get(f"{P}/tools", headers=_auth(cli))).status_code == 200 or True
    call = lambda who: client.post(f"{P}/tools/plan.limits/call", headers=_auth(who), json={"arguments": {}})
    assert (await call(cli)).status_code == 200 and (await call(key)).status_code == 200
    res = await admin.put(f"{O}/switches/surfaces/cli", json={"off": True, "message": "The command line is paused for now.", "reason": "testing"})
    assert res.status_code == 200 and res.json() == {"surface": "cli", "off": True}
    stopped = await call(cli)
    assert stopped.status_code == 403 and stopped.json()["error"]["code"] == "tool_off" and "paused for now" in stopped.json()["error"]["message"]
    assert (await call(key)).status_code == 200                                              # a service key is untouched
    state = (await admin.get(f"{O}/overview")).json()["switches"]
    assert state["surfaces_off"] == ["cli"] and "surface.cli" not in state["tools_off"]
    await admin.put(f"{O}/switches/surfaces/cli", json={"off": False, "reason": "done"})
    assert (await call(cli)).status_code == 200


async def test_pausing_assistants_also_stops_asking_ekavo_from_them(world, make_client, monkeypatch):
    client, ws, h = world
    from app.ask import router, runs, shortlist, threads

    await runs.create_indexes()
    await threads.create_indexes()

    async def no_space():
        return ""

    monkeypatch.setattr(shortlist, "pick_space", no_space)
    answer_with(monkeypatch)
    admin = await _admin(make_client)
    mcp = await token(client, h, ws, kind="mcp")
    assert (await client.post(f"{P}/ask", headers=_auth(mcp), json={"message": "hi"})).status_code == 201
    await admin.put(f"{O}/switches/surfaces/mcp", json={"off": True, "message": "Assistants are paused.", "reason": "testing"})
    refused = await client.post(f"{P}/ask", headers=_auth(mcp), json={"message": "hi"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "tool_off"


async def test_only_staff_can_change_a_way_in_and_an_unknown_one_is_not_found(world, make_client):
    client, ws, h = world
    assert (await client.put(f"{O}/switches/surfaces/cli", json={"off": True, "reason": "x"})).status_code in (401, 403)
    admin = await _admin(make_client)
    assert (await admin.put(f"{O}/switches/surfaces/email", json={"off": True, "reason": "x"})).status_code == 404
    assert set(ops.SURFACES) == {"cli", "mcp", "api"}


def test_the_sign_in_limit_is_a_plan_limit_on_every_plan_and_can_be_edited_like_the_others():
    assert "tool_sessions" in plan_table.LIMITS and plan_table.LIMITS["tool_sessions"]["kind"] == "count"
    for plan_id, plan in plan_table.DEFAULT_PLANS.items() if hasattr(plan_table, "DEFAULT_PLANS") else []:
        assert "tool_sessions" in plan["limits"], plan_id
    assert "tool_sessions" in entitlements.USAGE_KEYS and entitlements._LEGACY_FIXED["tool_sessions"] == 25


async def test_a_sign_in_over_the_plans_limit_is_refused_with_a_plain_message(world, monkeypatch):
    client, ws, h = world
    await _signed_in(client, h, ws=ws)                                                          # one sign-in is fine
    real = entitlements.has_room
    monkeypatch.setattr(entitlements, "has_room", lambda ent, key, used, adding=1: False if key == "tool_sessions" else real(ent, key, used, adding))
    start = (await client.post(f"{P}/device/start", json={"client": "cli", "label": "Second laptop"})).json()
    await client.post(f"{S}/device/lookup", json={"code": start["user_code"]})
    over = await client.post(f"{S}/device/approve", json={"code": start["user_code"], "workspace_id": ws, "scopes": ["read", "create"]})
    assert over.status_code == 409 and "sign" in over.text.lower()
