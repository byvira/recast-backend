"""OAuth sign-in for assistants that connect by address (Claude and others): discovery, introduction, the person's yes in the app, the code and its PKCE proof,
refresh with rotation, and every way it can be refused. It is a thin front over the sessions the command line already uses, so these tests also check that
the connection behaves like any other sign-in (listed, revocable, paused by the assistants switch). No model is called."""
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from app.core import staff
from app.db.mongo import users
from app.tools import oauth
from tests.conftest import signup_new_user
from tests.test_tools_public_api import P, _auth, world  # noqa: F401  (world is a fixture)
from tests.test_tools_sessions_mcp import S

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def register(client, redirects=(CALLBACK,), name="Claude") -> str:
    res = await client.post("/oauth/register", json={"client_name": name, "redirect_uris": list(redirects), "token_endpoint_auth_method": "none"})
    assert res.status_code == 201, res.text
    return res.json()["client_id"]


async def authorize(client, client_id, challenge, *, redirect=CALLBACK, scope="read create", state="st-1", **over):
    params = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
              "scope": scope, "state": state, "resource": oauth.resource_url(), **over}
    return await client.get("/oauth/authorize", params=params, follow_redirects=False)


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def connect(client, ws, *, scope="read create", scopes=("read", "create"), redirect=CALLBACK, registered=(CALLBACK,)):
    """The whole sign-in: introduce, send the person to the app, they say yes, the assistant trades the code. Returns (client_id, tokens, verifier)."""
    client_id = await register(client, registered)
    verifier, challenge = pkce()
    sent = await authorize(client, client_id, challenge, redirect=redirect, scope=scope)
    assert sent.status_code == 302, sent.text
    request_id = query(sent.headers["location"])["request"]
    yes = await client.post(f"{S}/oauth/approve", json={"request": request_id, "workspace_id": ws, "scopes": list(scopes)})
    assert yes.status_code == 200, yes.text
    back = query(yes.json()["redirect_url"])
    assert back["state"] == "st-1" and back["iss"] == oauth.api_base()
    got = await client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id, "code": back["code"], "code_verifier": verifier,
                                                    "redirect_uri": redirect, "resource": oauth.resource_url()})
    return client_id, got, verifier, back["code"]


async def mcp_list(client, token):
    return await client.post(f"{P}/mcp", headers=_auth(token), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


async def test_discovery_documents_say_where_to_sign_in_and_the_401_points_to_them(world):
    client, ws, h = world
    resource = (await client.get("/.well-known/oauth-protected-resource/api/public/v1/mcp")).json()
    assert resource["resource"] == oauth.resource_url() and resource["authorization_servers"] == [oauth.api_base()]
    meta = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert meta["issuer"] == oauth.api_base() and meta["code_challenge_methods_supported"] == ["S256"] and meta["token_endpoint_auth_methods_supported"] == ["none"]
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        assert meta[key].startswith(oauth.api_base() + "/oauth/")
    unauthorised = await client.post(f"{P}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert unauthorised.status_code == 401
    assert f'resource_metadata="{oauth.resource_metadata_url()}"' in unauthorised.headers["www-authenticate"]


async def test_the_whole_sign_in_gives_a_connection_that_works_lists_and_expires_like_any_other_sign_in(world):
    client, ws, h = world
    client_id, got, verifier, code = await connect(client, ws)
    assert got.status_code == 200, got.text
    tokens = got.json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["scope"] == "read create" and tokens["refresh_token"].startswith("ekr_")
    assert got.headers["cache-control"] == "no-store"
    listed = await mcp_list(client, tokens["access_token"])
    assert listed.status_code == 200 and listed.json()["result"]["tools"]
    rows = (await client.get(S, headers=h)).json()["items"]
    mine = [r for r in rows if r["client"] == "mcp" and r["label"] == "Claude"]
    assert len(mine) == 1 and mine[0]["scopes"] == ["read", "create"]
    ends = datetime.fromisoformat(mine[0]["expires_at"].replace("Z", "+00:00"))
    assert ends - datetime.now(timezone.utc) > timedelta(days=80)                       # the list shows when the connection ends, not the hour on the token


async def test_a_refresh_gives_new_tokens_in_place_and_the_old_ones_stop(world):
    client, ws, h = world
    client_id, got, *_ = await connect(client, ws)
    first = got.json()
    again = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": first["refresh_token"]})
    assert again.status_code == 200, again.text
    second = again.json()
    assert second["access_token"] != first["access_token"] and second["refresh_token"] != first["refresh_token"]
    assert (await mcp_list(client, first["access_token"])).status_code == 401
    assert (await mcp_list(client, second["access_token"])).status_code == 200
    rows = [r for r in (await client.get(S, headers=h)).json()["items"] if r["client"] == "mcp"]
    assert len(rows) == 1                                                               # replaced in place: the list does not fill up
    stale = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": first["refresh_token"]})
    assert stale.status_code == 400 and stale.json()["error"] == "invalid_grant"
    assert (await mcp_list(client, second["access_token"])).status_code == 200          # just now: refused, but the connection is not closed


async def test_an_old_refresh_token_shown_later_closes_the_whole_connection(world):
    from app.video import api_keys

    client, ws, h = world
    client_id, got, *_ = await connect(client, ws)
    first = got.json()
    second = (await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": first["refresh_token"]})).json()
    await api_keys._keys().update_many({"oauth.client_id": client_id}, {"$set": {"oauth.rotated_at": datetime.now(timezone.utc) - timedelta(minutes=5)}})
    replay = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": first["refresh_token"]})
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert (await mcp_list(client, second["access_token"])).status_code == 401
    assert (await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": second["refresh_token"]})).status_code == 400


async def test_a_code_works_once_and_using_it_again_closes_what_it_made(world):
    client, ws, h = world
    client_id, got, verifier, code = await connect(client, ws)
    token = got.json()["access_token"]
    twice = await client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id, "code": code, "code_verifier": verifier, "redirect_uri": CALLBACK})
    assert twice.status_code == 400 and twice.json()["error"] == "invalid_grant"
    assert (await mcp_list(client, token)).status_code == 401


async def test_the_proof_the_redirect_and_the_resource_must_all_match(world):
    client, ws, h = world
    client_id = await register(client)
    verifier, challenge = pkce()
    request_id = query((await authorize(client, client_id, challenge)).headers["location"])["request"]
    code = query((await client.post(f"{S}/oauth/approve", json={"request": request_id, "workspace_id": ws, "scopes": ["read"]})).json()["redirect_url"])["code"]
    base = {"grant_type": "authorization_code", "client_id": client_id, "code": code, "code_verifier": verifier, "redirect_uri": CALLBACK}
    wrong = await client.post("/oauth/token", data={**base, "code_verifier": secrets.token_urlsafe(48)})
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    moved = await client.post("/oauth/token", data={**base, "redirect_uri": "https://elsewhere.example/cb"})
    assert moved.json()["error"] == "invalid_grant"
    target = await client.post("/oauth/token", data={**base, "resource": "https://another.example/mcp"})
    assert target.json()["error"] == "invalid_target"
    other = await register(client, name="Other")
    stranger = await client.post("/oauth/token", data={**base, "client_id": other})
    assert stranger.json()["error"] == "invalid_grant"
    short = await client.post("/oauth/token", data={**base, "code_verifier": "short"})
    assert short.json()["error"] == "invalid_request"
    assert (await client.post("/oauth/token", data=base)).status_code == 200            # after all those, the right one still works


async def test_a_wrong_client_or_redirect_is_never_redirected_and_other_faults_go_back_to_the_assistant(world):
    client, ws, h = world
    _, challenge = pkce()
    unknown = await authorize(client, "ekc_nobody", challenge)
    assert unknown.status_code == 400 and unknown.json()["error"] == "invalid_client" and "location" not in unknown.headers
    client_id = await register(client)
    elsewhere = await authorize(client, client_id, challenge, redirect="https://evil.example/cb")
    assert elsewhere.status_code == 400 and "location" not in unknown.headers and unknown.json()["error"] == "invalid_client"
    assert elsewhere.json()["error"] == "invalid_request"
    for over, error in (({"code_challenge": ""}, "invalid_request"), ({"code_challenge_method": "plain"}, "invalid_request"), ({"response_type": "token"}, "unsupported_response_type"),
                        ({"resource": "https://another.example/mcp"}, "invalid_target")):
        sent = await authorize(client, client_id, challenge, **over)
        assert sent.status_code == 302 and sent.headers["location"].startswith(CALLBACK)
        back = query(sent.headers["location"])
        assert back["error"] == error and back["state"] == "st-1" and back["iss"] == oauth.api_base()


async def test_a_tool_on_this_computer_can_use_any_port_but_nothing_else_can(world):
    client, ws, h = world
    client_id, got, *_ = await connect(client, ws, redirect="http://localhost:53122/callback", registered=("http://localhost/callback", "http://127.0.0.1/callback"))
    assert got.status_code == 200, got.text
    client_id = await register(client, ("http://localhost/callback",))
    _, challenge = pkce()
    wrong_path = await authorize(client, client_id, challenge, redirect="http://localhost:53122/other")
    other_host = await authorize(client, client_id, challenge, redirect="http://127.0.0.1:53122/callback")
    assert wrong_path.status_code == 400 and other_host.status_code == 400


async def test_introductions_are_checked(world):
    client, ws, h = world
    bad = [{"redirect_uris": ["http://example.com/cb"]}, {"redirect_uris": ["https://x.example/cb#frag"]}, {"redirect_uris": []}, {"redirect_uris": "https://x.example/cb"},
           {"redirect_uris": ["https://x.example/cb"], "token_endpoint_auth_method": "client_secret_post"}, {"redirect_uris": ["https://user:pw@x.example/cb"]},
           {"redirect_uris": ["https://x.example/cb"], "grant_types": ["client_credentials"]}, {"redirect_uris": [f"https://x.example/{i}" for i in range(6)]}]
    for body in bad:
        res = await client.post("/oauth/register", json=body)
        assert res.status_code == 400 and res.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata"), body
    assert (await client.post("/oauth/register", content=b"nope", headers={"content-type": "application/json"})).status_code == 400
    named = await client.post("/oauth/register", json={"client_name": "  <b>Evil</b>\n\x00 app  ", "redirect_uris": [CALLBACK]})
    assert named.json()["client_name"] == "bEvil/b app"


async def test_the_person_sees_who_is_asking_where_it_goes_and_can_say_no(world):
    client, ws, h = world
    client_id = await register(client)
    _, challenge = pkce()
    request_id = query((await authorize(client, client_id, challenge, scope="read create publish")).headers["location"])["request"]
    seen = (await client.post(f"{S}/oauth/lookup", json={"request": request_id})).json()
    assert seen["client_name"] == "Claude" and seen["redirect_host"] == "claude.ai" and seen["on_this_computer"] is False and seen["scopes"] == ["read", "create", "publish"]
    assert any(w["id"] == ws for w in seen["workspaces"])
    no = await client.post(f"{S}/oauth/deny", json={"request": request_id})
    back = query(no.json()["redirect_url"])
    assert back["error"] == "access_denied" and back["state"] == "st-1" and "code" not in back
    again = await client.post(f"{S}/oauth/approve", json={"request": request_id, "workspace_id": ws, "scopes": ["read"]})
    assert again.status_code == 404                                                     # answered once, and a request runs out


async def test_a_connection_can_never_have_more_than_the_person_may_do(world, make_client):
    client, ws, h = world
    client_id = await register(client)
    _, challenge = pkce()
    request_id = query((await authorize(client, client_id, challenge)).headers["location"])["request"]
    admin = await client.post(f"{S}/oauth/approve", json={"request": request_id, "workspace_id": ws, "scopes": ["read", "admin"]})
    assert admin.status_code == 422 or admin.status_code == 403
    approve = await client.post(f"{S}/oauth/approve", json={"request": request_id, "workspace_id": ws, "scopes": ["approve"]})
    assert approve.status_code == 403                                                   # a person's own decisions stay theirs unless they tick the box
    stranger = make_client()
    await signup_new_user(stranger, name="Stranger")
    outsider = await stranger.post(f"{S}/oauth/approve", json={"request": request_id, "workspace_id": ws, "scopes": ["read"]})
    assert outsider.status_code == 403                                                  # not a member of that workspace
    assert (await client.post(f"{S}/oauth/approve", json={"request": request_id, "workspace_id": ws, "scopes": ["approve"], "allow_approve": True})).status_code == 200


async def test_revoking_in_settings_or_pausing_assistants_stops_the_connection_and_the_assistant_can_sign_itself_out(world, make_client):
    client, ws, h = world
    client_id, got, *_ = await connect(client, ws)
    tokens = got.json()
    session_id = next(r["id"] for r in (await client.get(S, headers=h)).json()["items"] if r["client"] == "mcp")
    admin = make_client()
    user = await signup_new_user(admin, name="Staff")
    await users.update_one({"id": user["id"]}, {"$set": {"is_platform_staff": True, "is_master_admin": True, "staff": {"level": "admin"}}})
    staff.forget(user["id"])
    await admin.put("/api/v1/ops/tools/switches/surfaces/mcp", json={"off": True, "message": "Assistants are paused.", "reason": "testing"})
    paused = await client.post(f"{P}/mcp", headers=_auth(tokens["access_token"]), json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "plan.limits", "arguments": {}}})
    assert paused.status_code in (200, 403) and "paused" in paused.text.lower()
    await admin.put("/api/v1/ops/tools/switches/surfaces/mcp", json={"off": False, "reason": "done"})
    assert (await client.post(f"{S}/{session_id}/revoke", headers=h)).status_code == 200
    assert (await mcp_list(client, tokens["access_token"])).status_code == 401
    ended = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]})
    assert ended.status_code == 400 and ended.json()["error"] == "invalid_grant"
    client_id, got, *_ = await connect(client, ws)
    fresh = got.json()
    assert (await client.post("/oauth/revoke", data={"token": fresh["refresh_token"]})).status_code == 200
    assert (await mcp_list(client, fresh["access_token"])).status_code == 401
    assert (await client.post("/oauth/revoke", data={"token": "never-issued"})).status_code == 200


async def test_other_ways_of_asking_for_a_token_are_turned_away(world):
    client, ws, h = world
    for grant in ("client_credentials", "password", ""):
        res = await client.post("/oauth/token", data={"grant_type": grant, "client_id": "x"})
        assert res.status_code == 400 and res.json()["error"] == "unsupported_grant_type"
    assert (await client.post("/oauth/token", json={"grant_type": "refresh_token"})).json()["error"] == "unsupported_grant_type"        # the standard says form encoded
    unknown = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "ekc_nobody", "refresh_token": "x"})
    assert unknown.status_code == 401 and unknown.json()["error"] == "invalid_client"


async def test_the_public_reference_lists_every_tool_a_key_could_use_in_plain_terms_without_a_key(world):
    from app.tools.registry import get_registry

    client, ws, h = world
    res = await client.get(f"{P}/reference")
    assert res.status_code == 200 and "max-age" in res.headers["cache-control"]
    body = res.json()
    usable = [t for t in get_registry().all() if not t.human_only]
    assert body["count"] == len(usable) == len(body["tools"]) and body["server"] == "/api/public/v1"
    entry = next(t for t in body["tools"] if t["name"] == usable[0].name)
    assert set(entry) >= {"name", "group", "summary", "risk", "scope", "mode", "cost", "approval", "undo", "examples", "fields", "beta"}
    kinds = {f["kind"] for t in body["tools"] for f in t["fields"]}
    assert kinds and not any("$ref" in k or "{" in k for k in kinds)                  # plain words, never a schema fragment
    assert all(t["name"] != n.name for t in body["tools"] for n in get_registry().all() if n.human_only)


async def test_too_many_new_apps_in_a_day_are_refused_until_tomorrow(world, monkeypatch):
    client, ws, h = world
    from app.db.mongo import get_db

    await get_db()["oauth_clients"].delete_many({})
    monkeypatch.setattr(oauth, "MAX_REGISTRATIONS_A_DAY", 2)
    for _ in range(2):
        await register(client)
    third = await client.post("/oauth/register", json={"client_name": "One too many", "redirect_uris": [CALLBACK]})
    assert third.status_code == 503 and third.json()["error"] == "temporarily_unavailable"
    await get_db()["oauth_clients"].update_many({}, {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(days=2)}})
    assert (await client.post("/oauth/register", json={"client_name": "Tomorrow", "redirect_uris": [CALLBACK]})).status_code == 201


async def test_a_refresh_token_cannot_be_used_by_a_different_app(world):
    client, ws, h = world
    client_id, got, *_ = await connect(client, ws)
    other = await register(client, name="Other app")
    stolen = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": other, "refresh_token": got.json()["refresh_token"]})
    assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_grant"
    assert (await mcp_list(client, got.json()["access_token"])).status_code == 200                  # the real connection is untouched
    assert (await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": got.json()["refresh_token"]})).status_code == 200
