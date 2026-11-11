"""The public tools API end to end: keys made in Settings, the tool list a key sees, the OpenAPI document, calls done by the app's own routes,
idempotency, dry runs, a person's yes for publishing, and stable errors. No credits are spent and no provider is called."""

import json
from uuid import uuid4

import pytest

from app.db.mongo import get_db
from app.tools import ledger
from app.tools.registry import get_registry
from tests.conftest import create_workspace

D = "/api/v1/developer"
P = "/api/public/v1"


@pytest.fixture
async def world(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Tools {uuid4().hex[:6]}")
    await ledger.create_indexes()
    await ledger.settings().delete_many({})
    return client, ws, {"X-Workspace-Id": ws}


async def _key(client, h, scopes=("read",), capabilities=(), name="Zapier") -> str:
    made = await client.post(f"{D}/tool-keys", headers=h, json={"name": name, "scopes": list(scopes), "capabilities": list(capabilities)})
    assert made.status_code == 201, made.text
    return made.json()["secret"]


def _auth(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _preset(**over) -> dict:
    from typing import get_args

    annotation = get_registry().get("presets.create").input_model.model_fields["category"].annotation
    first = next(iter(get_args(annotation) or list(annotation)))                    # a Literal's first value, or an enum's
    first = getattr(first, "value", first)
    return {"title": "Launch post", "category": first, "category_label": "Launch", **over}


# ── keys made in Settings ────────────────────────────────────────────────

async def test_a_tool_key_is_made_listed_measured_and_revoked_in_settings(world):
    client, ws, h = world
    secret = await _key(client, h, scopes=("read", "create"), capabilities=("delete",))
    listed = (await client.get(f"{D}/tool-keys", headers=h)).json()
    assert len(listed["keys"]) == 1 and "secret" not in listed["keys"][0] and "hash" not in listed["keys"][0]
    assert listed["keys"][0]["tool_scopes"] == ["read", "create"] and listed["keys"][0]["capabilities"] == ["delete"] and listed["keys"][0]["credit_cap"] == 100
    key_id = listed["keys"][0]["id"]
    await client.post(f"{P}/tools/presets.list/call", headers=_auth(secret), json={"arguments": {}})
    usage = (await client.get(f"{D}/tool-keys/{key_id}/usage", headers=h)).json()
    assert usage["credit_cap"] == 100 and usage["credits_this_month"] == 0 and usage["calls"]
    assert (await client.delete(f"{D}/keys/{key_id}", headers=h)).status_code == 204
    assert (await client.get(f"{P}/tools", headers=_auth(secret))).status_code == 401           # a revoke acts on the next call


async def test_a_key_can_never_be_admin_and_a_bad_request_is_refused_in_plain_words(world):
    client, ws, h = world
    for body in ({"name": "x", "scopes": ["admin"]}, {"name": "x", "scopes": ["read", "approve"]}, {"name": "x", "scopes": ["read"], "capabilities": ["root"]},
                 {"name": "  ", "scopes": ["read"]}):
        assert (await client.post(f"{D}/tool-keys", headers=h, json=body)).status_code in (400, 422)


# ── what a key can see ───────────────────────────────────────────────────

async def test_every_bad_key_gets_the_same_401_with_a_stable_code(world):
    client, ws, h = world
    for headers in ({}, _auth("ek_live_nope"), _auth("nonsense")):
        for method, url in (("GET", f"{P}/tools"), ("GET", f"{P}/tools/presets.list"), ("POST", f"{P}/tools/presets.list/call")):
            res = await client.request(method, url, headers=headers, json={"arguments": {}} if method == "POST" else None)
            assert res.status_code == 401 and res.json()["error"]["code"] == "auth_invalid" and res.json()["request_id"]


async def test_the_tool_list_follows_the_keys_scopes_capabilities_and_the_persons_role(world):
    client, ws, h = world
    read_only = (await client.get(f"{P}/tools", headers=_auth(await _key(client, h)))).json()
    assert read_only["count"] > 100 and {t["risk"] for t in read_only["tools"]} == {"read"}

    making = (await client.get(f"{P}/tools", headers=_auth(await _key(client, h, scopes=("read", "create"), name="Maker")))).json()
    names = {t["name"] for t in making["tools"]}
    assert "presets.create" in names and "publish.now" not in names and "presets.delete" not in names       # no publish, no delete

    deleting = (await client.get(f"{P}/tools", headers=_auth(await _key(client, h, scopes=("read", "create"), capabilities=("delete",), name="Deleter")))).json()
    assert "presets.delete" in {t["name"] for t in deleting["tools"]}
    assert "brand.set_logo" not in names and "approve" not in {t["scope"] for t in making["tools"]}


async def test_one_tool_shows_its_typed_input_and_a_hidden_one_looks_like_a_missing_one(world):
    client, ws, h = world
    secret = await _key(client, h)
    shown = (await client.get(f"{P}/tools/presets.list", headers=_auth(secret))).json()
    assert shown["name"] == "presets.list" and shown["input"]["type"] == "object" and shown["available"] is True
    hidden = await client.get(f"{P}/tools/presets.create", headers=_auth(secret))
    missing = await client.get(f"{P}/tools/nothing.here", headers=_auth(secret))
    assert hidden.status_code == missing.status_code == 404 and hidden.json()["error"]["code"] == "tool_unknown"


async def test_the_openapi_document_has_one_operation_per_tool_and_every_reference_resolves(world):
    client, ws, h = world
    doc = (await client.get(f"{P}/openapi.json")).json()
    registry = get_registry()
    assert doc["openapi"].startswith("3.1") and len(doc["paths"]) == len(registry)
    assert {f"/tools/{t.name}/call" for t in registry.all()} == set(doc["paths"])
    schemas = doc["components"]["schemas"]
    for ref in {r.split("/")[-1] for r in __import__("re").findall(r'"\$ref": "([^"]+)"', json.dumps(doc))}:
        assert ref in schemas, ref


# ── calls ────────────────────────────────────────────────────────────────

async def test_a_read_tool_is_answered_by_the_apps_own_route_and_written_to_the_ledger(world):
    client, ws, h = world
    secret = await _key(client, h)
    res = await client.post(f"{P}/tools/presets.list/call", headers=_auth(secret), json={"arguments": {}})
    assert res.status_code == 200 and res.json()["ok"] is True and res.json()["estimate"] == {"credits": 0, "exact": True}
    row = await ledger.calls().find_one({"tool": "presets.list", "workspace_id": ws})
    assert row["outcome"] == "done" and row["surface"] == "api" and row["fields"] == []


async def test_a_repeated_request_is_answered_from_the_first_and_made_once(world):
    client, ws, h = world
    secret = await _key(client, h, scopes=("read", "create"))
    headers = {**_auth(secret), "Idempotency-Key": "launch-1"}
    first = await client.post(f"{P}/tools/presets.create/call", headers=headers, json={"arguments": _preset()})
    second = await client.post(f"{P}/tools/presets.create/call", headers=headers, json={"arguments": _preset()})
    assert first.status_code in (200, 201) and second.json()["replayed"] is True and second.json()["data"] == first.json()["data"]
    assert await get_db()["presets"].count_documents({"workspace_id": ws}) == 1
    other = await client.post(f"{P}/tools/presets.create/call", headers=headers, json={"arguments": _preset(title="Another")})
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"
    assert await get_db()["presets"].count_documents({"workspace_id": ws}) == 1


async def test_a_dry_run_shows_the_estimate_and_changes_nothing(world):
    client, ws, h = world
    secret = await _key(client, h, scopes=("read", "create"))
    res = await client.post(f"{P}/tools/presets.create/call", headers=_auth(secret), json={"arguments": _preset(), "dry_run": True})
    assert res.status_code == 200 and res.json()["dry_run"] is True and res.json()["estimate"]["credits"] == 0
    assert await get_db()["presets"].count_documents({"workspace_id": ws}) == 0


async def test_bad_input_is_refused_with_the_fields_named_before_anything_runs(world):
    client, ws, h = world
    secret = await _key(client, h, scopes=("read", "create"))
    res = await client.post(f"{P}/tools/presets.create/call", headers=_auth(secret), json={"arguments": {"title": ""}})
    assert res.status_code == 422 and res.json()["error"]["code"] == "input_invalid" and res.json()["error"]["problems"]
    assert await get_db()["presets"].count_documents({"workspace_id": ws}) == 0


async def test_an_error_from_the_app_comes_back_in_its_own_words_and_frees_the_key_for_a_retry(world):
    client, ws, h = world
    secret = await _key(client, h)
    headers = {**_auth(secret), "Idempotency-Key": "look-1"}
    res = await client.post(f"{P}/tools/runs.get/call", headers=headers, json={"arguments": {"run_id": "nope"}})
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found" and res.json()["error"]["message"]
    again = await client.post(f"{P}/tools/runs.get/call", headers=headers, json={"arguments": {"run_id": "nope"}})
    assert again.json()["error"]["code"] == "not_found"                                    # not "request in progress": the key was freed


async def test_every_listed_tool_has_a_runner_and_one_without_says_so(world, monkeypatch):
    from app.tools import executor

    client, ws, h = world
    secret = await _key(client, h, scopes=("read", "create"), name="Editor")
    listed = (await client.get(f"{P}/tools", headers=_auth(secret))).json()["tools"]
    assert listed and all(t["available"] for t in listed)                          # nothing is listed that cannot be run

    executor.load_handlers()
    monkeypatch.setattr(executor, "HANDLERS", {k: v for k, v in executor.HANDLERS.items() if k != "image.reset"})
    res = await client.post(f"{P}/tools/image.reset/call", headers=_auth(secret), json={"arguments": {"image_asset_id": "x", "base_version": 1}})
    assert res.status_code == 501 and res.json()["error"]["code"] == "tool_not_wired"


async def test_a_key_cannot_do_what_its_scope_does_not_allow(world):
    client, ws, h = world
    secret = await _key(client, h)
    res = await client.post(f"{P}/tools/presets.create/call", headers=_auth(secret), json={"arguments": _preset()})
    assert res.status_code == 403 and res.json()["error"]["code"] == "scope_missing"


# ── publishing needs a person ────────────────────────────────────────────

async def test_a_key_cannot_publish_without_a_persons_yes_and_cannot_override_a_safety_hold(world):
    client, ws, h = world
    secret = await _key(client, h, scopes=("read", "create", "publish"))
    piece = uuid4().hex
    await get_db()["content_pieces"].insert_one({"id": piece, "piece_id": piece, "workspace_id": ws, "flagged_for_review": False})
    call = lambda **a: client.post(f"{P}/tools/publish.now/call", headers=_auth(secret), json={"arguments": {"piece_id": piece, **a}})

    no_yes = await call()
    assert no_yes.status_code == 409 and no_yes.json()["error"]["code"] == "approval_required"
    override = await call(confirm_publish_anyway=True)
    assert override.status_code == 403 and override.json()["error"]["code"] == "human_only"


async def test_a_person_gives_the_yes_in_the_app_and_a_key_cannot_give_it_to_itself(world):
    client, ws, h = world
    secret = await _key(client, h, scopes=("read", "create", "publish"))
    piece, held = uuid4().hex, uuid4().hex
    await get_db()["content_pieces"].insert_many([
        {"id": piece, "piece_id": piece, "workspace_id": ws, "flagged_for_review": False},
        {"id": held, "piece_id": held, "workspace_id": ws, "flagged_for_review": True},
    ])
    ok = await client.post("/api/v1/tool-approvals", headers=h, json={"tool": "publish.now", "arguments": {"piece_id": piece}})
    assert ok.status_code == 201 and ok.json()["valid_minutes"] == ledger.APPROVAL_MINUTES and len(ok.json()["approval_token"]) >= 32
    assert (await client.post("/api/v1/tool-approvals", headers=h, json={"tool": "publish.now", "arguments": {"piece_id": held}})).status_code == 409
    assert (await client.post("/api/v1/tool-approvals", headers=h, json={"tool": "presets.list", "arguments": {}})).status_code == 404
    dry = await client.post(f"{P}/tools/publish.now/call", headers=_auth(secret),
                            json={"arguments": {"piece_id": piece}, "approval_token": ok.json()["approval_token"], "dry_run": True})
    assert dry.status_code == 200 and dry.json()["dry_run"] is True                      # a dry run does not use up the yes
    assert await ledger.approvals().count_documents({"workspace_id": ws, "used": False}) == 1

    client.cookies.clear()                                                                   # now only the key is left: it is not a person
    key_try = await client.post("/api/v1/tool-approvals", headers={**_auth(secret), **h}, json={"tool": "publish.now", "arguments": {"piece_id": piece}})
    assert key_try.status_code in (401, 403)


async def test_a_publish_call_waits_for_a_person_who_sees_the_exact_post_and_the_key_collects_the_yes(world):
    client, ws, h = world
    secret = await _key(client, h, scopes=("read", "create", "publish"))
    piece = uuid4().hex
    await get_db()["content_pieces"].insert_one({"piece_id": piece, "id": piece, "workspace_id": ws, "platform": "LinkedIn", "content": "We launch on Friday.",
                                                 "flagged_for_review": False})
    asked = await client.post(f"{P}/tools/publish.now/call", headers=_auth(secret), json={"arguments": {"piece_id": piece}})
    assert asked.status_code == 409 and asked.json()["error"]["code"] == "approval_required"
    request_id = asked.json()["error"]["approval_request_id"]
    again = await client.post(f"{P}/tools/publish.now/call", headers=_auth(secret), json={"arguments": {"piece_id": piece}})
    assert again.json()["error"]["approval_request_id"] == request_id                              # the same call is one request

    waiting = (await client.get("/api/v1/tool-approvals/pending", headers=h)).json()["requests"]
    assert [r["id"] for r in waiting] == [request_id]
    shown = waiting[0]["preview"]
    assert shown["posts"][0]["text"] == "We launch on Friday." and shown["posts"][0]["platform"] == "LinkedIn" and waiting[0]["tool"] == "publish.now"

    poll = lambda: client.get(f"{P}/approvals/{request_id}", headers=_auth(secret))
    assert (await poll()).json()["request"]["status"] == "pending" and "approval_token" not in (await poll()).json()["request"]
    yes = await client.post(f"/api/v1/tool-approvals/{request_id}/decide", headers=h, json={"approve": True})
    assert yes.status_code == 200 and yes.json()["request"]["status"] == "approved"
    token = (await poll()).json()["request"]["approval_token"]
    dry = await client.post(f"{P}/tools/publish.now/call", headers=_auth(secret), json={"arguments": {"piece_id": piece}, "approval_token": token, "dry_run": True})
    assert dry.status_code == 200 and dry.json()["dry_run"] is True                               # the token fits exactly this call
    assert (await client.get("/api/v1/tool-approvals/pending", headers=h)).json()["requests"] == []
    assert (await client.post(f"/api/v1/tool-approvals/{request_id}/decide", headers=h, json={"approve": False})).status_code == 404   # answered once


async def test_a_no_ends_the_request_and_one_key_cannot_read_anothers(world):
    client, ws, h = world
    mine, other = await _key(client, h, scopes=("read", "publish"), name="Mine"), await _key(client, h, scopes=("read", "publish"), name="Other")
    piece = uuid4().hex
    await get_db()["content_pieces"].insert_one({"piece_id": piece, "id": piece, "workspace_id": ws, "platform": "X", "content": "Hi", "flagged_for_review": False})
    request_id = (await client.post(f"{P}/tools/publish.now/call", headers=_auth(mine), json={"arguments": {"piece_id": piece}})).json()["error"]["approval_request_id"]
    assert (await client.get(f"{P}/approvals/{request_id}", headers=_auth(other))).status_code == 404
    assert (await client.post(f"/api/v1/tool-approvals/{request_id}/decide", headers=h, json={"approve": False})).json()["request"]["status"] == "declined"
    seen = (await client.get(f"{P}/approvals/{request_id}", headers=_auth(mine))).json()["request"]
    assert seen["status"] == "declined" and "approval_token" not in seen
