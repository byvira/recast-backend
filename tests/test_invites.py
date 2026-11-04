"""Integration tests for the Invites endpoints, including RBAC and
lifecycle (expired / already-accepted / already-member / revoked) negative
cases.
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
    # In dev/test mode, sending is short-circuited and always "succeeds".
    assert res.json()["email_sent"] is True


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
    invite_email = unique_email()
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": invite_email, "role": "editor"}
    )
    token = res.json()["token"]

    joiner = make_client()
    joiner_profile = await signup_new_user(joiner, email=invite_email)
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
    invite_email = unique_email()
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": invite_email, "role": "viewer"}
    )
    token = res.json()["token"]

    joiner = make_client()
    await signup_new_user(joiner, email=invite_email)
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 200

    # Same token again by the same person: they are already in, so it just confirms, and adds nothing
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 200
    members = (await api_client.get(f"/api/v1/workspaces/{ws_id}/members")).json()["items"]
    assert len([m for m in members if m["email"] == invite_email]) == 1
    # someone else holding the used link gets nothing
    stranger = make_client()
    await signup_new_user(stranger)
    assert (await stranger.post(f"/api/v1/invites/accept/{token}")).status_code == 404


async def test_accept_invite_already_member_conflict(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Already Member Space", tier="large")

    member_client, member_profile = await invite_and_accept(api_client, make_client, ws_id, "editor")

    # A second, still-pending invite to that same (already-a-member) email.
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": member_profile["email"], "role": "viewer"}
    )
    # Someone who is already a member is refused when the invite is made, so no second invite exists to accept.
    assert res.status_code == 409
    accepted_again = await member_client.post("/api/v1/invites/accept/not-a-real-token")
    assert accepted_again.status_code == 404


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


async def test_accept_invite_email_mismatch_rejected(api_client, make_client):
    """Regression test for a flagged-but-unfixed gap from earlier in this
    module: any authenticated user holding the token could accept as
    themselves, regardless of whether they were the invited person."""
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Email Mismatch Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    token = res.json()["token"]

    wrong_person = make_client()
    await signup_new_user(wrong_person)  # a different, unrelated random email
    res = await wrong_person.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 403

    # The invite is untouched — still acceptable by the actual invited email.
    res = await api_client.get(f"/api/v1/invites/{ws_id}")
    assert res.json()["items"][0]["status"] == "pending"


async def test_revoke_invite_requires_invite_members_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Revoke Perms Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]

    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")
    res = await viewer_client.delete(f"/api/v1/invites/{ws_id}/{invite_id}")
    assert res.status_code == 403

    res = await api_client.delete(f"/api/v1/invites/{ws_id}/{invite_id}")
    assert res.status_code == 200
    assert res.json()["revoked"] is True


async def test_revoke_invite_removes_it_from_pending_list(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Revoke List Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]

    res = await api_client.get(f"/api/v1/invites/{ws_id}")
    assert len(res.json()["items"]) == 1

    res = await api_client.delete(f"/api/v1/invites/{ws_id}/{invite_id}")
    assert res.status_code == 200

    res = await api_client.get(f"/api/v1/invites/{ws_id}")
    assert res.json()["items"] == []


async def test_revoked_invite_cannot_be_accepted(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Revoke Then Accept Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]
    token = res.json()["token"]

    res = await api_client.delete(f"/api/v1/invites/{ws_id}/{invite_id}")
    assert res.status_code == 200

    joiner = make_client()
    await signup_new_user(joiner)
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 404


async def test_revoke_invite_not_found(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Revoke 404 Space", tier="large")
    res = await api_client.delete(f"/api/v1/invites/{ws_id}/not-a-real-invite-id")
    assert res.status_code == 404


async def test_revoke_already_accepted_invite_rejected(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Revoke Accepted Space", tier="large")
    invite_email = unique_email()
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": invite_email, "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]
    token = res.json()["token"]

    joiner = make_client()
    await signup_new_user(joiner, email=invite_email)
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 200

    res = await api_client.delete(f"/api/v1/invites/{ws_id}/{invite_id}")
    assert res.status_code == 400


async def test_resend_invite_requires_invite_members_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Resend Perms Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]

    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")
    res = await viewer_client.post(f"/api/v1/invites/{ws_id}/{invite_id}/resend")
    assert res.status_code == 403

    res = await api_client.post(f"/api/v1/invites/{ws_id}/{invite_id}/resend")
    assert res.status_code == 200
    assert res.json()["invite_id"] == invite_id
    assert res.json()["email_sent"] is True


async def test_resend_invite_keeps_same_token_and_extends_expiry(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Resend Token Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]
    original_token = res.json()["token"]

    await invites_collection.update_one(
        {"id": invite_id},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}},
    )

    res = await api_client.post(f"/api/v1/invites/{ws_id}/{invite_id}/resend")
    assert res.status_code == 200

    doc = await invites_collection.find_one({"id": invite_id})
    assert doc["token"] == original_token
    assert doc["status"] == "pending"
    # Motor returns naive datetimes from Mongo even though we wrote tz-aware ones.
    stored_expiry = doc["expires_at"].replace(tzinfo=timezone.utc)
    assert stored_expiry > datetime.now(timezone.utc)

    # The (previously expired, now refreshed) link works again.
    res = await api_client.get(f"/api/v1/invites/accept/{original_token}")
    assert res.status_code == 200


async def test_resend_invite_not_found(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Resend 404 Space", tier="large")
    res = await api_client.post(f"/api/v1/invites/{ws_id}/not-a-real-invite-id/resend")
    assert res.status_code == 404


async def test_resend_already_accepted_invite_rejected(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Resend Accepted Space", tier="large")
    invite_email = unique_email()
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": invite_email, "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]
    token = res.json()["token"]

    joiner = make_client()
    await signup_new_user(joiner, email=invite_email)
    res = await joiner.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 200

    res = await api_client.post(f"/api/v1/invites/{ws_id}/{invite_id}/resend")
    assert res.status_code == 400


async def test_resend_revoked_invite_rejected(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Resend Revoked Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]

    await api_client.delete(f"/api/v1/invites/{ws_id}/{invite_id}")

    res = await api_client.post(f"/api/v1/invites/{ws_id}/{invite_id}/resend")
    assert res.status_code == 400


async def test_list_pending_excludes_chronologically_expired(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Expired Pending Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    invite_id = res.json()["invite_id"]
    await invites_collection.update_one(
        {"id": invite_id},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}},
    )

    # Stored status is still literally "pending" — nothing proactively flips it.
    doc = await invites_collection.find_one({"id": invite_id})
    assert doc["status"] == "pending"

    res = await api_client.get(f"/api/v1/invites/{ws_id}")
    assert res.status_code == 200
    assert res.json()["items"] == []


async def test_list_all_shows_full_history_with_effective_status(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "History Space", tier="large")

    # Accepted
    accepted_invite_email = unique_email()
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": accepted_invite_email, "role": "viewer"}
    )
    accepted_token = res.json()["token"]
    joiner = make_client()
    await signup_new_user(joiner, email=accepted_invite_email)
    res = await joiner.post(f"/api/v1/invites/accept/{accepted_token}")
    assert res.status_code == 200

    # Revoked
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    revoked_id = res.json()["invite_id"]
    await api_client.delete(f"/api/v1/invites/{ws_id}/{revoked_id}")

    # Chronologically expired (stored status still "pending")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    expired_id = res.json()["invite_id"]
    await invites_collection.update_one(
        {"id": expired_id},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}},
    )

    # Still pending
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    pending_id = res.json()["invite_id"]

    res = await api_client.get(f"/api/v1/invites/{ws_id}", params={"status": "all"})
    assert res.status_code == 200
    by_id = {item["id"]: item["status"] for item in res.json()["items"]}
    assert by_id[revoked_id] == "revoked"
    assert by_id[expired_id] == "expired"
    assert by_id[pending_id] == "pending"
    accepted_statuses = [
        v for k, v in by_id.items() if k not in (revoked_id, expired_id, pending_id)
    ]
    assert accepted_statuses == ["accepted"]


async def test_create_invite_reports_email_delivery_failure(api_client, monkeypatch):
    """create_invite never 500s over an email provider hiccup (the invite is
    still created either way) — but the caller needs to know delivery
    failed, since the only real fallback is sharing the link directly."""
    import app.api.v1.invites as invites_module

    async def fake_send_templated_email(*args, **kwargs):
        return False

    monkeypatch.setattr(invites_module, "send_templated_email", fake_send_templated_email)

    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Email Failure Space", tier="large")
    res = await api_client.post(
        f"/api/v1/invites/{ws_id}", json={"email": unique_email(), "role": "viewer"}
    )
    assert res.status_code == 201
    assert res.json()["email_sent"] is False

    # The invite itself was still created and is usable.
    res = await api_client.get(f"/api/v1/invites/{ws_id}")
    assert len(res.json()["items"]) == 1


def test_an_invite_carries_the_plan_chosen_on_the_pricing_page():
    from app.shared.invites import invite_url, plan_from_source

    assert invite_url("abc", plan_from_source("pricing-large")).endswith("/signup?invite=abc&plan=large")
    assert invite_url("abc", plan_from_source("waitlist-page")).endswith("/signup?invite=abc")
    assert plan_from_source("pricing-free") is None and plan_from_source(None) is None


async def test_someone_invited_before_they_have_an_account_joins_at_sign_up_without_a_workspace_of_their_own(api_client, make_client):
    from app.db.mongo import activity_entries, workspace_members, workspaces

    owner = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Invite First", tier="large")
    invite_email = unique_email()
    assert (await api_client.post(f"/api/v1/invites/{ws_id}", json={"email": invite_email, "role": "viewer"})).status_code == 201

    joiner = make_client()
    profile = await signup_new_user(joiner, email=invite_email)
    # in the workspace with the role they were invited with, and no starter workspace of their own
    member = await workspace_members.find_one({"workspace_id": ws_id, "user_id": profile["id"]})
    assert member and member["role"] == "viewer"
    assert await workspaces.count_documents({"owner_id": profile["id"]}) == 0
    items = (await joiner.get("/api/v1/workspaces/")).json()["items"]
    assert [w["workspace_id"] for w in items] == [ws_id]


async def test_the_person_who_sent_the_invite_is_who_added_the_member(api_client, make_client, monkeypatch):
    from app.shared import governance_events

    seen = []
    monkeypatch.setattr(governance_events, "emit_member_added", lambda ws, **kw: seen.append(kw))
    owner = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Who Added", tier="large")
    invite_email = unique_email()
    await api_client.post(f"/api/v1/invites/{ws_id}", json={"email": invite_email, "role": "viewer"})
    joiner = make_client()
    profile = await signup_new_user(joiner, email=invite_email)
    assert seen and seen[-1]["actor_user_id"] == owner["id"] and seen[-1]["actor_role"] == "owner"
    assert seen[-1]["subject_user_id"] == profile["id"] and seen[-1]["role"] == "viewer"
