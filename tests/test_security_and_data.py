"""Access and data rules: who can invite whom, how invites and seats are counted, what deleting a workspace does, and that
deleted pictures stop being reachable. Talks to the test database; no provider is called."""
from datetime import datetime, timedelta, timezone

from app.db.mongo import image_assets, image_share_links, invites, users, workspace_members, workspaces
from tests.conftest import create_workspace, invite_and_accept, signup_new_user, unique_email


async def test_an_admin_cannot_invite_someone_as_owner(make_client, api_client):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Team", tier="duo")
    admin_client, _ = await invite_and_accept(api_client, make_client, ws, "admin")
    res = await admin_client.post(f"/api/v1/invites/{ws}", json={"email": unique_email(), "role": "owner"})
    assert res.status_code == 403
    ok = await admin_client.post(f"/api/v1/invites/{ws}", json={"email": unique_email(), "role": "editor"})
    assert ok.status_code in (201, 400)  # 400 only if the tier has no free seat


async def test_the_same_person_cannot_be_invited_twice_and_bad_emails_are_refused(api_client):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Team", tier="duo")
    email = unique_email()
    first = await api_client.post(f"/api/v1/invites/{ws}", json={"email": email, "role": "editor"})
    assert first.status_code in (201, 400)
    if first.status_code == 201:
        again = await api_client.post(f"/api/v1/invites/{ws}", json={"email": email, "role": "editor"})
        assert again.status_code == 409
    bad = await api_client.post(f"/api/v1/invites/{ws}", json={"email": "not-an-address", "role": "editor"})
    assert bad.status_code == 422


async def test_an_expired_invite_no_longer_holds_a_seat(api_client):
    from app.api.v1.invites import _seats_used

    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Team", tier="duo")
    before = await _seats_used(ws, datetime.now(timezone.utc))
    old = datetime.now(timezone.utc) - timedelta(days=8)
    await invites.insert_one({
        "id": "inv-expired", "workspace_id": ws, "email": unique_email(), "role": "editor", "token": "t-expired",
        "status": "pending", "invited_by": "x", "created_at": old, "expires_at": old + timedelta(days=7),
    })
    assert await _seats_used(ws, datetime.now(timezone.utc)) == before


async def test_accepting_an_invite_twice_never_makes_a_server_error(make_client, api_client):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Team", tier="duo")
    email = unique_email()
    sent = await api_client.post(f"/api/v1/invites/{ws}", json={"email": email, "role": "editor"})
    assert sent.status_code == 201, sent.text
    token = sent.json()["token"]
    member = make_client()
    await signup_new_user(member, email=email)
    first = await member.post(f"/api/v1/invites/accept/{token}")
    second = await member.post(f"/api/v1/invites/accept/{token}")
    assert first.status_code == 200
    assert second.status_code in (404, 409)


async def test_deleting_a_workspace_hides_it_keeps_it_and_gives_everyone_a_working_default(make_client, api_client):
    profile = await signup_new_user(api_client)
    ws = await create_workspace(api_client, "To remove", tier="duo")
    member_client, member = await invite_and_accept(api_client, make_client, ws, "editor")
    await users.update_one({"id": member["id"]}, {"$set": {"default_workspace_id": ws}})

    res = await api_client.delete(f"/api/v1/workspaces/{ws}")
    assert res.status_code == 200 and res.json()["restorable_until"]
    stored = await workspaces.find_one({"id": ws})
    assert stored and stored.get("deleted_at") and stored.get("purge_after")  # kept, not removed
    assert await workspace_members.count_documents({"workspace_id": ws}) >= 2

    listed = (await api_client.get("/api/v1/workspaces/")).json()
    assert ws not in [w["workspace_id"] for w in listed["items"]]
    refused = await api_client.get("/api/v1/brand/", headers={"X-Workspace-Id": ws})
    assert refused.status_code == 404

    moved = await users.find_one({"id": member["id"]})
    assert moved["default_workspace_id"] != ws  # a workspace that still exists (their personal one)
    after = await member_client.get("/api/v1/brand/")
    assert after.status_code == 200  # no "no workspace context" error


async def test_a_deleted_workspace_can_be_restored_by_its_owner_only(make_client, api_client):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Oops", tier="duo")
    member_client, _ = await invite_and_accept(api_client, make_client, ws, "admin")
    await api_client.delete(f"/api/v1/workspaces/{ws}")
    assert (await member_client.post(f"/api/v1/workspaces/{ws}/restore")).status_code == 403
    assert (await api_client.post(f"/api/v1/workspaces/{ws}/restore")).status_code == 200
    assert not (await workspaces.find_one({"id": ws})).get("deleted_at")


async def test_the_purge_job_removes_a_workspace_past_its_hold_with_everything_it_owned(api_client):
    from app.workers.workspace_purge import purge_workspace

    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Gone for good", tier="duo")
    await image_assets.insert_one({"id": "img-purge", "workspace_id": ws, "title": "x"})
    removed = await purge_workspace(ws)
    assert removed.get("image_assets") == 1
    assert await workspaces.find_one({"id": ws}) is None
    assert await workspace_members.count_documents({"workspace_id": ws}) == 0
    assert await image_assets.find_one({"id": "img-purge"}) is None


async def test_logging_out_works_for_a_session_whose_access_token_has_expired(api_client):
    await signup_new_user(api_client)
    api_client.cookies.delete("access_token")  # expired and gone from the browser; the refresh cookie is still there
    res = await api_client.post("/api/v1/auth/logout")
    assert res.status_code == 200
    again = await api_client.post("/api/v1/auth/logout")
    assert again.status_code == 401  # nothing left to end


async def test_a_deleted_picture_cannot_be_opened_or_shared(api_client):
    await signup_new_user(api_client)
    now = datetime.now(timezone.utc)
    me = (await api_client.get("/api/v1/users/me")).json()
    ws = me["default_workspace_id"]
    await image_assets.insert_one({
        "id": "img-del", "workspace_id": ws, "brand_id": "b", "created_by": me["id"], "title": "t", "slides": [], "version_count": 1,
        "created_at": now, "updated_at": now,
    })
    await image_share_links.insert_one({
        "token": "tok-del", "image_asset_id": "img-del", "workspace_id": ws, "created_by": me["id"], "created_at": now,
        "expires_at": now + timedelta(days=5), "revoked": False,
    })
    deleted = await api_client.delete("/api/v1/image-assets/img-del")
    assert deleted.status_code == 200
    assert (await api_client.get("/api/v1/image-assets/img-del")).status_code == 404
    assert (await api_client.post("/api/v1/image-assets/img-del/share")).status_code == 404
    assert (await image_share_links.find_one({"token": "tok-del"}))["revoked"] is True
    assert (await api_client.get("/api/v1/share/tok-del")).status_code == 404
