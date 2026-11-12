"""Browser sign-in for the command line and assistants, and the MCP server. No provider is called and no credits are spent."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.mongo import get_db
from app.tools import ledger, sessions
from app.tools.registry import get_registry
from tests.test_tools_public_api import P, _auth, _key, world  # noqa: F401  (world is a fixture)

S = "/api/v1/tool-sessions"


async def _signed_in(client, h, scopes=("read", "create"), client_kind="cli", label="Work laptop", ws=None):
    """Runs the whole flow: the tool asks for a code, the person approves it in the app, the tool collects the token."""
    start = await client.post(f"{P}/device/start", json={"client": client_kind, "label": label})
    assert start.status_code == 201, start.text
    begun = start.json()
    code = begun["user_code"]
    look = await client.post(f"{S}/device/lookup", json={"code": code})
    assert look.status_code == 200, look.text
    workspace = ws or look.json()["workspaces"][0]["id"]
    approved = await client.post(f"{S}/device/approve", json={"code": code, "workspace_id": workspace, "scopes": list(scopes)})
    assert approved.status_code == 200, approved.text
    got = await client.post(f"{P}/device/token", json={"device_code": begun["device_code"]})
    return begun, got


# ── the sign-in flow ─────────────────────────────────────────────────────────

async def test_the_whole_sign_in_gives_a_session_token_once_and_the_token_works(world):
    client, ws, h = world
    begun, got = await _signed_in(client, h, ws=ws)
    body = got.json()
    assert got.status_code == 200 and body["status"] == "approved" and body["token_type"] == "Bearer" and body["workspace_id"] == ws
    assert set(body["scopes"]) == {"read", "create"} and body["access_token"].startswith("ek_live_")
    again = await client.post(f"{P}/device/token", json={"device_code": begun["device_code"]})
    assert again.status_code == 410 and again.json()["error"]["code"] == "expired"          # the token is handed over once

    listed = await client.get(f"{P}/tools", headers=_auth(body["access_token"]))
    assert listed.status_code == 200 and listed.json()["count"] > 0
    tool = next(t for t in get_registry().all() if t.risk.value == "read" and not t.cost_key and not (t.input_schema() or {}).get("required"))
    res = await client.post(f"{P}/tools/{tool.name}/call", headers=_auth(body["access_token"]), json={"arguments": {}})
    assert res.status_code == 200, res.text
    row = await ledger.calls().find_one({"workspace_id": ws, "tool": tool.name, "outcome": {"$in": ["done", "admitted"]}})
    assert row["surface"] == "cli"                                                              # recorded as made from the command line


async def test_nothing_secret_is_kept_in_the_clear(world):
    client, ws, h = world
    begun, got = await _signed_in(client, h, ws=ws)
    stored = [d async for d in sessions.codes().find({})]
    text = str(stored)
    assert begun["device_code"] not in text and begun["user_code"].replace("-", "") not in text and got.json()["access_token"] not in text
    keys = [d async for d in get_db()["api_keys"].find({"workspace_id": ws})]
    assert got.json()["access_token"] not in str(keys)


async def test_polling_too_fast_is_told_to_slow_down_and_a_pending_code_stays_pending(world):
    client, ws, h = world
    begun = (await client.post(f"{P}/device/start", json={"client": "mcp", "label": "Claude"})).json()
    first = await client.post(f"{P}/device/token", json={"device_code": begun["device_code"]})
    second = await client.post(f"{P}/device/token", json={"device_code": begun["device_code"]})
    assert first.json()["status"] == "pending" and second.json()["status"] == "slow_down" and second.json()["interval"] > first.json()["interval"]


async def test_a_wrong_expired_denied_or_over_tried_code_is_refused_in_plain_words(world):
    client, ws, h = world
    assert (await client.post(f"{S}/device/lookup", json={"code": "ZZZZ-ZZZZ"})).status_code == 404
    assert (await client.post(f"{S}/device/lookup", json={"code": "12"})).status_code == 422

    begun = (await client.post(f"{P}/device/start", json={"client": "cli", "label": "x"})).json()
    await sessions.codes().update_many({}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    assert (await client.post(f"{S}/device/lookup", json={"code": begun["user_code"]})).status_code == 404
    assert (await client.post(f"{P}/device/token", json={"device_code": begun["device_code"]})).status_code == 410

    denied = (await client.post(f"{P}/device/start", json={"client": "cli", "label": "y"})).json()
    assert (await client.post(f"{S}/device/deny", json={"code": denied["user_code"]})).status_code == 200
    gone = await client.post(f"{P}/device/token", json={"device_code": denied["device_code"]})
    assert gone.status_code == 403 and gone.json()["error"]["code"] == "denied"

    tried = (await client.post(f"{P}/device/start", json={"client": "cli", "label": "z"})).json()
    for _ in range(sessions.MAX_LOOKUPS):
        assert (await client.post(f"{S}/device/lookup", json={"code": tried["user_code"]})).status_code == 200
    assert (await client.post(f"{S}/device/lookup", json={"code": tried["user_code"]})).status_code == 404       # closed after too many tries


async def test_a_sign_in_can_never_manage_things_or_approve_unless_the_person_ticks_the_box(world):
    client, ws, h = world
    begun = (await client.post(f"{P}/device/start", json={"client": "cli", "label": "x"})).json()
    base = {"code": begun["user_code"], "workspace_id": ws}
    admin = await client.post(f"{S}/device/approve", json={**base, "scopes": ["read", "admin"]})
    assert admin.status_code == 403 and "never manage" in admin.json()["detail"]
    approve = await client.post(f"{S}/device/approve", json={**base, "scopes": ["read", "approve"]})
    assert approve.status_code == 403 and "Tick that box" in approve.json()["detail"]
    ticked = await client.post(f"{S}/device/approve", json={**base, "scopes": ["read", "approve"], "allow_approve": True})
    assert ticked.status_code == 200 and "approve" in ticked.json()["scopes"]
    assert (await client.post(f"{S}/device/approve", json={**base, "scopes": ["read"]})).status_code in (404, 409)   # already answered


async def test_a_workspace_the_person_is_not_in_is_refused(world):
    client, ws, h = world
    begun = (await client.post(f"{P}/device/start", json={"client": "cli", "label": "x"})).json()
    res = await client.post(f"{S}/device/approve", json={"code": begun["user_code"], "workspace_id": "someone-elses", "scopes": ["read"]})
    assert res.status_code == 403


async def test_sessions_are_listed_apart_from_keys_and_revoking_one_stops_it_at_once(world):
    client, ws, h = world
    await _key(client, h)
    begun, got = await _signed_in(client, h, ws=ws, label="Work laptop")
    token = got.json()["access_token"]
    listed = (await client.get(S, headers=h)).json()
    assert [i["label"] for i in listed["items"]] == ["Work laptop"] and listed["items"][0]["client"] == "cli" and "access_token" not in str(listed)
    keys = (await client.get("/api/v1/developer/tool-keys", headers=h)).json()["keys"]
    assert all(k["name"] != "Work laptop" for k in keys) and len(keys) == 1                      # a session is not a key
    assert (await client.post(f"{S}/{listed['items'][0]['id']}/revoke", headers=h)).status_code == 200
    assert (await client.get(f"{P}/tools", headers=_auth(token))).status_code == 401
    assert (await client.post(f"{S}/nope/revoke", headers=h)).status_code == 404


# ── MCP ──────────────────────────────────────────────────────────────────────

def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params is not None else {})}


async def _mcp(client, token, message):
    return await client.post(f"{P}/mcp", headers=_auth(token), json=message)


async def test_mcp_needs_a_key_or_session_and_answers_the_handshake(world):
    client, ws, h = world
    assert (await client.post(f"{P}/mcp", json=_rpc("ping"))).status_code == 401
    assert (await client.post(f"{P}/mcp", headers=_auth("nope"), json=_rpc("ping"))).status_code == 401
    _, got = await _signed_in(client, h, ws=ws, client_kind="mcp", label="Claude")
    token = got.json()["access_token"]
    hello = (await _mcp(client, token, _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}))).json()
    assert hello["result"]["protocolVersion"] == "2025-06-18" and hello["result"]["serverInfo"]["name"] == "ekavo" and "tools" in hello["result"]["capabilities"]
    assert (await _mcp(client, token, {"jsonrpc": "2.0", "method": "notifications/initialized"})).status_code == 202
    assert (await _mcp(client, token, _rpc("ping"))).json()["result"] == {}
    assert (await client.get(f"{P}/mcp", headers=_auth(token))).status_code == 405


async def test_mcp_lists_the_tools_the_session_may_use_with_hints_and_runs_them_like_the_rest_api(world):
    client, ws, h = world
    _, got = await _signed_in(client, h, ws=ws, scopes=("read",), client_kind="mcp", label="Cursor")
    token = got.json()["access_token"]
    listed = (await _mcp(client, token, _rpc("tools/list"))).json()["result"]["tools"]
    names = {t["name"] for t in listed}
    rest = {t["name"] for t in (await client.get(f"{P}/tools", headers=_auth(token))).json()["tools"] if t["available"]}
    assert names == rest and len(names) > 20
    sample = next(t for t in listed if t["annotations"]["readOnlyHint"])
    assert sample["inputSchema"]["type"] == "object" and sample["description"]

    tool = next(t for t in get_registry().all() if t.name in names and t.risk.value == "read" and not t.cost_key and not (t.input_schema() or {}).get("required"))
    called = (await _mcp(client, token, _rpc("tools/call", {"name": tool.name, "arguments": {}}))).json()["result"]
    rest_call = (await client.post(f"{P}/tools/{tool.name}/call", headers=_auth(token), json={"arguments": {}})).json()
    assert called["isError"] is False and called["structuredContent"]["data"] == rest_call["data"] and called["content"][0]["type"] == "text"
    row = await ledger.calls().find_one({"workspace_id": ws, "tool": tool.name, "surface": "mcp"})
    assert row is not None                                                                      # recorded as made over MCP


async def test_mcp_refuses_what_the_session_was_not_given_in_the_same_stable_words_and_shows_a_price_on_a_dry_run(world):
    client, ws, h = world
    _, got = await _signed_in(client, h, ws=ws, scopes=("read",), client_kind="mcp", label="Cursor")
    token = got.json()["access_token"]
    make = next(t for t in get_registry().all() if t.scope.value == "create" and not t.human_only and not (t.input_schema() or {}).get("required"))
    refused = (await _mcp(client, token, _rpc("tools/call", {"name": make.name, "arguments": {}}))).json()["result"]
    rest = (await client.post(f"{P}/tools/{make.name}/call", headers=_auth(token), json={"arguments": {}})).json()
    assert refused["isError"] is True and refused["structuredContent"]["error"]["code"] == rest["error"]["code"] and refused["content"][0]["text"]

    _, wider = await _signed_in(client, h, ws=ws, scopes=("read", "create"), client_kind="mcp", label="Cursor 2")
    dry = (await _mcp(client, wider.json()["access_token"], _rpc("tools/call", {"name": make.name, "arguments": {}, "_meta": {"dry_run": True}}))).json()["result"]
    assert dry["structuredContent"].get("dry_run") is True or dry["isError"] is True            # a price, or the same plain refusal of what is missing from the input
    assert await ledger.calls().count_documents({"workspace_id": ws, "tool": make.name, "outcome": "done"}) == 0


async def test_mcp_answers_a_bad_message_with_a_json_rpc_error_not_a_crash(world):
    client, ws, h = world
    _, got = await _signed_in(client, h, ws=ws, client_kind="mcp", label="Claude")
    token = got.json()["access_token"]
    bad = await client.post(f"{P}/mcp", headers={**_auth(token), "Content-Type": "application/json"}, content=b"{nope")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700
    assert (await _mcp(client, token, [_rpc("ping")])).json()["error"]["code"] == -32600          # one message at a time
    assert (await _mcp(client, token, _rpc("nothing/here"))).json()["error"]["code"] == -32601
    assert (await _mcp(client, token, _rpc("tools/call", {"name": 5, "arguments": {}}))).json()["error"]["code"] == -32602
    assert (await _mcp(client, token, _rpc("tools/call", {"name": "no.such_tool", "arguments": {}}))).json()["result"]["isError"] is True
