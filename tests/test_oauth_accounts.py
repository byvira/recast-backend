"""Integration tests for GET /api/v1/oauth/accounts.

Scoped narrowly to the list endpoint and its connected_by_name enrichment —
the actual OAuth connect/callback flows talk to real third-party providers
(Meta, Google, etc.) and aren't exercised here; that's out of this module's
scope. Connections are inserted directly into workspace_connections to test
the read path in isolation.
"""

from datetime import datetime, timedelta, timezone

from app.db.mongo import workspace_connections
from tests.conftest import create_workspace, signup_new_user


async def test_list_accounts_empty_for_fresh_workspace(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "OAuth Accounts Space")

    res = await api_client.get("/api/v1/oauth/accounts", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200
    body = res.json()
    assert body["accounts"] == []
    assert body["total"] == 0


async def test_list_accounts_includes_connected_by_name(api_client):
    profile = await signup_new_user(api_client, name="Connector Person")
    ws_id = await create_workspace(api_client, "OAuth Enrichment Space")

    now = datetime.now(timezone.utc)
    await workspace_connections.insert_one(
        {
            "id": "conn-test-1",
            "workspace_id": ws_id,
            "platform": "linkedin",
            "platform_user_id": "li-123",
            "username": "connector",
            "is_active": True,
            "connected_by": profile["id"],
            "connected_at": now,
            "expires_at": now + timedelta(days=60),
            "access_token_encrypted": "unused-in-this-test",
        }
    )

    res = await api_client.get("/api/v1/oauth/accounts", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    account = body["accounts"][0]
    assert account["platform"] == "linkedin"
    assert account["connected_by"] == profile["id"]
    assert account["connected_by_name"] == "Connector Person"


async def test_list_accounts_requires_workspace_membership(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "OAuth Access Guard Space")

    outsider = make_client()
    await signup_new_user(outsider)
    res = await outsider.get("/api/v1/oauth/accounts", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 403


async def test_an_older_threads_connection_is_asked_to_reconnect_once(api_client):
    """Threads connected before the insights permission posts fine but cannot read stats: the list says to reconnect it once. A
    connection with the permission, and every other platform, are never flagged."""
    from app.api.v1.oauth import THREADS_SCOPES

    profile = await signup_new_user(api_client, name="Threads Person")
    old_ws = await create_workspace(api_client, "Old Threads Space")
    new_ws = await create_workspace(api_client, "New Threads Space")
    now = datetime.now(timezone.utc)
    base = {"platform_user_id": "th-1", "username": "poster", "is_active": True, "connected_by": profile["id"], "connected_at": now}
    await workspace_connections.insert_one({**base, "id": "th-old", "workspace_id": old_ws, "platform": "threads"})
    await workspace_connections.insert_one({**base, "id": "li-old", "workspace_id": old_ws, "platform": "linkedin"})
    await workspace_connections.insert_one({**base, "id": "th-new", "workspace_id": new_ws, "platform": "threads", "scopes": THREADS_SCOPES.split(",")})

    old = {a["platform"]: a for a in (await api_client.get("/api/v1/oauth/accounts", headers={"X-Workspace-Id": old_ws})).json()["accounts"]}
    assert old["threads"]["reconnect_for"] == "insights" and old["linkedin"]["reconnect_for"] is None
    new = (await api_client.get("/api/v1/oauth/accounts", headers={"X-Workspace-Id": new_ws})).json()["accounts"]
    assert new[0]["reconnect_for"] is None
