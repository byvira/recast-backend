"""Integration tests for the 4 Invites endpoints, including RBAC and
lifecycle (expired / already-accepted / already-member) negative cases.
"""

from datetime import datetime, timedelta, timezone

import httpx

from app.db.mongo import invites as invites_collection
from app.main import app
from tests.conftest import create_workspace, invite_and_accept, signup_new_user, unique_email


def _anon_client() -> httpx.AsyncClient:
    """An unauthenticated client — no cookies, no prior signup."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_send_invite_requires_invite_members_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Invite Perms", tier="large")
    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "editor"}
    )
    assert res.status_code == 403

    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "editor"}
    )
    assert res.status_code == 201
    assert res.json()["token"]


async def test_send_invite_enforces_seat_limit(api_client):
    await signup_new_user(api_client)
    # duo tier = 2 seats; owner already occupies 1.
    ws_id = await create_workspace(api_client, "Small Team", tier="duo")

    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "editor"}
    )
    assert res.status_code == 201

    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "editor"}
    )
    assert res.status_code == 400


async def test_list_invites_requires_invite_members_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "List Perms", tier="large")
    await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "editor"}
    )

    editor_client, _ = await invite_and_accept(api_client, make_client, ws_id, "editor")
    res = await editor_client.get(f"/api/v1/invites/{ws_id}")
    assert res.status_code == 403

    res = await api_client.get(f"/api/v1/invites/{ws_id}")
    assert res.status_code == 200
    # Only the standalone invite above is still pending — the editor's own
    # invite was already accepted by invite_and_accept, so list_invites
    # (pending-only) doesn't include it.
    assert len(res.json()["items"]) == 1


async def test_preview_invite_public_no_auth(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Preview Space", tier="large")
    invite_email = unique_email()
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": invite_email, "role": "viewer"}
    )
    token = res.json()["token"]

    async with _anon_client() as anon:
        res = await anon.get(f"/api/v1/invites/accept/{token}")
        assert res.status_code == 200
        body = res.json()
        assert body["workspace_name"] == "Preview Space"
        assert body["role"] == "viewer"
        assert body["email"] == invite_email


async def test_preview_invite_not_found(api_client):
    res = await api_client.get("/api/v1/invites/accept/not-a-real-token")
    assert res.status_code == 404


async def test_preview_invite_expired(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Expiry Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    token = res.json()["token"]

    await invites_collection.update_one(
        {"token": token},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}},
    )

    res = await api_client.get(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 410


async def test_accept_invite_requires_auth(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Auth Required Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    token = res.json()["token"]

    async with _anon_client() as anon:
        res = await anon.post(f"/api/v1/invites/accept/{token}")
        assert res.status_code == 401


async def test_accept_invite_success_adds_member_with_invited_role(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Join Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "editor"}
    )
    token = res.json()["token"]

    joiner = make_client()
    joiner_profile = await signup_new_user(joiner)
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 200
    assert res.json()["workspace_id"] == ws_id
    assert res.json()["role"] == "editor"

    res = await api_client.get(f"/api/v1/workspaces/{ws_id}/members")
    member = next(
        m for m in res.json()["items"] if m["user_id"] == joiner_profile["id"]
    )
    assert member["role"] == "editor"


async def test_accept_invite_twice_rejected(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Double Accept Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    token = res.json()["token"]

    joiner = make_client()
    await signup_new_user(joiner)
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 200

    # Same token again — invite status is no longer "pending".
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 404


async def test_accept_invite_already_member_conflict(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Already Member Space", tier="large")

    member_client, _ = await invite_and_accept(api_client, make_client, ws_id, "editor")

    # A second, still-pending invite that resolves to a user already in the workspace.
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    token = res.json()["token"]

    res = await member_client.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 409


async def test_accept_invite_expired_rejected(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Expired Accept Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    token = res.json()["token"]
    await invites_collection.update_one(
        {"token": token},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}},
    )

    joiner = make_client()
    await signup_new_user(joiner)
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 410
