"""Integration tests for the 8 Workspace endpoints, including RBAC negatives.

Every negative case here asserts the *backend* rejects the action — per the
module's ground rules, frontend role-gating is a UX nicety, not the
enforcement boundary.
"""

from tests.conftest import create_workspace, invite_and_accept, signup_new_user


async def test_create_workspace_adds_owner_membership(api_client):
    profile = await signup_new_user(api_client)
    res = await api_client.post(
        "/api/v1/workspaces/", json={"name": "Acme Media Lab", "tier": "duo"}
    )
    assert res.status_code == 201
    body = res.json()
    assert body["tier"] == "duo"
    assert body["tier_config"]["seats"] == 2
    workspace_id = body["workspace_id"]

    res = await api_client.get(f"/api/v1/workspaces/{workspace_id}/members")
    assert res.status_code == 200
    members = res.json()["items"]
    assert len(members) == 1
    assert members[0]["user_id"] == profile["id"]
    assert members[0]["role"] == "owner"
    # Enriched fields (added in this module) — the frontend Team tab needs these.
    assert members[0]["name"] == profile["name"]
    assert members[0]["email"] == profile["email"]


async def test_list_workspaces_includes_personal_and_created(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Team Space")

    res = await api_client.get("/api/v1/workspaces/")
    assert res.status_code == 200
    items = res.json()["items"]
    ws_ids = {i["workspace_id"] for i in items}
    assert ws_id in ws_ids
    # Personal workspace auto-created at signup should also be listed.
    assert len(items) >= 2
    assert any(i["is_personal"] for i in items)


async def test_get_workspace_requires_membership(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Private Space")

    res = await api_client.get(f"/api/v1/workspaces/{ws_id}")
    assert res.status_code == 200
    assert res.json()["name"] == "Private Space"

    outsider = make_client()
    await signup_new_user(outsider)
    res = await outsider.get(f"/api/v1/workspaces/{ws_id}")
    assert res.status_code == 403


async def test_get_workspace_not_found(api_client):
    await signup_new_user(api_client)
    res = await api_client.get("/api/v1/workspaces/does-not-exist")
    assert res.status_code == 404


async def test_update_workspace_owner_only(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Original Name", tier="large")

    res = await api_client.patch(
        f"/api/v1/workspaces/{ws_id}", json={"name": "Renamed", "language": "fr"}
    )
    assert res.status_code == 200
    res = await api_client.get(f"/api/v1/workspaces/{ws_id}")
    assert res.json()["name"] == "Renamed"
    assert res.json()["language"] == "fr"

    # admin lacks manage_workspace_settings — must be rejected server-side.
    admin_client, _ = await invite_and_accept(api_client, make_client, ws_id, "admin")
    res = await admin_client.patch(f"/api/v1/workspaces/{ws_id}", json={"name": "Hijacked"})
    assert res.status_code == 403


async def test_update_workspace_partial_patch_does_not_clobber_other_field(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Keep My Name")
    await api_client.patch(f"/api/v1/workspaces/{ws_id}", json={"language": "es"})

    res = await api_client.get(f"/api/v1/workspaces/{ws_id}")
    body = res.json()
    assert body["name"] == "Keep My Name"
    assert body["language"] == "es"


async def test_update_workspace_no_fields_rejected(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Space")
    res = await api_client.patch(f"/api/v1/workspaces/{ws_id}", json={})
    assert res.status_code == 400


async def test_delete_workspace_owner_only(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Deletable")

    admin_client, _ = await invite_and_accept(api_client, make_client, ws_id, "admin")
    res = await admin_client.delete(f"/api/v1/workspaces/{ws_id}")
    assert res.status_code == 403

    res = await api_client.delete(f"/api/v1/workspaces/{ws_id}")
    assert res.status_code == 200
    assert res.json()["deleted"] is True

    res = await api_client.get(f"/api/v1/workspaces/{ws_id}")
    assert res.status_code == 404


async def test_delete_personal_workspace_rejected(api_client):
    profile = await signup_new_user(api_client)
    personal_ws_id = profile["default_workspace_id"]
    res = await api_client.delete(f"/api/v1/workspaces/{personal_ws_id}")
    assert res.status_code == 400


async def test_set_member_role_owner_only(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Roles Test", tier="large")
    admin_client, _ = await invite_and_accept(api_client, make_client, ws_id, "admin")
    _, editor_profile = await invite_and_accept(api_client, make_client, ws_id, "editor")

    # admin cannot change roles — manage_roles is owner-only.
    res = await admin_client.put(
        f"/api/v1/workspaces/{ws_id}/members/{editor_profile['id']}/role",
        json={"role": "viewer"},
    )
    assert res.status_code == 403

    # owner can.
    res = await api_client.put(
        f"/api/v1/workspaces/{ws_id}/members/{editor_profile['id']}/role",
        json={"role": "viewer"},
    )
    assert res.status_code == 200
    assert res.json()["role"] == "viewer"


async def test_cannot_change_owner_role(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Owner Guard")
    res = await api_client.get(f"/api/v1/workspaces/{ws_id}/members")
    owner_user_id = res.json()["items"][0]["user_id"]

    res = await api_client.put(
        f"/api/v1/workspaces/{ws_id}/members/{owner_user_id}/role",
        json={"role": "admin"},
    )
    assert res.status_code == 400


async def test_remove_member_owner_and_admin_only(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Remove Test", tier="large")
    viewer_client, viewer_profile = await invite_and_accept(api_client, make_client, ws_id, "viewer")
    _, editor_profile = await invite_and_accept(api_client, make_client, ws_id, "editor")

    # A viewer has no remove_members permission.
    res = await viewer_client.delete(
        f"/api/v1/workspaces/{ws_id}/members/{editor_profile['id']}"
    )
    assert res.status_code == 403

    # The owner does.
    res = await api_client.delete(
        f"/api/v1/workspaces/{ws_id}/members/{viewer_profile['id']}"
    )
    assert res.status_code == 200
    assert res.json()["removed"] is True

    res = await api_client.get(f"/api/v1/workspaces/{ws_id}/members")
    remaining_ids = {m["user_id"] for m in res.json()["items"]}
    assert viewer_profile["id"] not in remaining_ids


async def test_cannot_remove_owner(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Owner Removal Guard")
    admin_client, _ = await invite_and_accept(api_client, make_client, ws_id, "admin")

    res = await api_client.get(f"/api/v1/workspaces/{ws_id}/members")
    owner_user_id = next(
        m["user_id"] for m in res.json()["items"] if m["role"] == "owner"
    )

    res = await admin_client.delete(f"/api/v1/workspaces/{ws_id}/members/{owner_user_id}")
    assert res.status_code == 400


async def test_non_member_cannot_list_members(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Members Guard")

    outsider = make_client()
    await signup_new_user(outsider)
    res = await outsider.get(f"/api/v1/workspaces/{ws_id}/members")
    assert res.status_code == 403
