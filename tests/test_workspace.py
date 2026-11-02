"""Integration tests for the 8 Workspace endpoints, including RBAC negatives.

Every negative case here asserts the *backend* rejects the action — per the
module's ground rules, frontend role-gating is a UX nicety, not the
enforcement boundary.
"""

from tests.conftest import create_workspace, invite_and_accept, signup_new_user


async def test_create_workspace_adds_owner_membership(api_client):
    profile = await signup_new_user(api_client)
    res = await api_client.post(
        "/api/v1/workspaces/", json={"name": "Acme Media Lab", "tier": "single"}
    )
    assert res.status_code == 201
    body = res.json()
    assert body["tier"] == "single"
    assert body["tier_config"]["seats"] == 1
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


async def test_list_workspaces_includes_the_first_one_and_created(api_client):
    profile = await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Team Space")

    res = await api_client.get("/api/v1/workspaces/")
    assert res.status_code == 200
    items = res.json()["items"]
    ws_ids = {i["workspace_id"] for i in items}
    assert ws_id in ws_ids
    # the workspace made at signup is an ordinary one, waiting to be set up
    first = next(i for i in items if i["workspace_id"] == profile["default_workspace_id"])
    assert first["is_personal"] is False and first["setup_pending"] is True


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


async def test_a_persons_only_workspace_cannot_be_deleted(api_client):
    profile = await signup_new_user(api_client)
    first = profile["default_workspace_id"]
    res = await api_client.delete(f"/api/v1/workspaces/{first}")
    assert res.status_code == 400 and "only workspace" in res.json()["detail"]
    # with another one, the first can go
    await create_workspace(api_client, "Second")
    assert (await api_client.delete(f"/api/v1/workspaces/{first}")).status_code == 200


async def test_onboarding_sets_up_the_first_workspace_instead_of_making_a_second(api_client, make_client):
    profile = await signup_new_user(api_client)
    first = profile["default_workspace_id"]
    # Duo is a paid plan: choosing it before paying is refused, and the workspace is set up as Single
    refused = await api_client.post(f"/api/v1/workspaces/{first}/setup", json={"name": "Asha Studio", "tier": "duo", "purpose": "work"})
    assert refused.status_code == 402 and refused.json()["detail"]["code"] == "plan_needed"
    res = await api_client.post(f"/api/v1/workspaces/{first}/setup", json={"name": "Asha Studio", "tier": "single", "purpose": "work"})
    assert res.status_code == 200, res.text
    items = (await api_client.get("/api/v1/workspaces/")).json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Asha Studio" and items[0]["tier"] == "single" and items[0]["setup_pending"] is False
    assert items[0]["purpose"] == "work"
    # the label can change any time
    assert (await api_client.patch(f"/api/v1/workspaces/{first}", json={"purpose": "personal"})).status_code == 200
    assert (await api_client.get("/api/v1/workspaces/")).json()["items"][0]["purpose"] == "personal"
    # a setup without the choice is refused
    assert (await api_client.post(f"/api/v1/workspaces/{first}/setup", json={"name": "x", "tier": "single"})).status_code in (409, 422)
    detail = (await api_client.get(f"/api/v1/workspaces/{first}")).json()
    assert detail["tier_config"]["seats"] == 1
    # once only, and only by its owner
    assert (await api_client.post(f"/api/v1/workspaces/{first}/setup", json={"name": "Again", "tier": "single", "purpose": "work"})).status_code == 409
    other = make_client()
    await signup_new_user(other)
    assert (await other.post(f"/api/v1/workspaces/{first}/setup", json={"name": "Mine", "tier": "single", "purpose": "personal"})).status_code in (403, 404)


async def test_with_plans_on_a_free_person_sets_up_their_workspace_and_a_second_waits_for_a_plan(api_client, monkeypatch):
    from app.core.config import settings
    from app.shared import entitlements

    monkeypatch.setattr(settings, "BILLING_ENFORCED", True)
    entitlements.forget()
    profile = await signup_new_user(api_client)
    first = profile["default_workspace_id"]
    # onboarding never runs into the one-workspace limit
    assert (await api_client.post(f"/api/v1/workspaces/{first}/setup", json={"name": "Solo", "tier": "single", "purpose": "personal"})).status_code == 200
    # a second one on Free is the upgrade moment
    res = await api_client.post("/api/v1/workspaces/", json={"name": "Second brand", "tier": "single"})
    assert res.status_code == 402 and res.json()["detail"]["limit"] == "owned_workspaces"
    entitlements.forget()


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



async def test_a_team_size_comes_only_with_the_plan_paid_for(api_client):
    from app.billing import subscriptions as S
    from app.db.mongo import workspaces

    await signup_new_user(api_client)
    for tier in ("duo", "large"):
        res = await api_client.post("/api/v1/workspaces/", json={"name": f"Team {tier}", "tier": tier})
        assert res.status_code == 402 and res.json()["detail"]["plan"] == tier
    paid = (await api_client.post("/api/v1/workspaces/", json={"name": "Paid", "tier": "single"})).json()["workspace_id"]
    # once a plan is paid for, the size follows it; a workspace sharing that plan may take the size too
    assert S._size_for("duo")["tier"] == "duo" and S._size_for("free")["tier"] == "single"
    await workspaces.update_one({"id": paid}, {"$set": {"plan": "duo"}})
    shared = await api_client.post("/api/v1/workspaces/", json={"name": "Client", "tier": "duo", "billing_group_id": paid})
    assert shared.status_code == 201, shared.text
    assert (await api_client.post("/api/v1/workspaces/", json={"name": "Too big", "tier": "large", "billing_group_id": paid})).status_code == 402



async def test_another_workspace_needs_a_paid_plan_even_with_plan_limits_off(api_client, monkeypatch):
    from app.api.v1 import workspace as workspace_routes
    from app.db.mongo import workspaces

    monkeypatch.setattr(workspace_routes, "EXTRA_WORKSPACES_NEED_PLAN", True)
    profile = await signup_new_user(api_client)
    first = profile["default_workspace_id"]
    # the free workspace made at sign-up is the one free workspace
    res = await api_client.post("/api/v1/workspaces/", json={"name": "Second", "tier": "single"})
    assert res.status_code == 402 and res.json()["detail"]["limit"] == "owned_workspaces"
    cover = (await api_client.get("/api/v1/billing/coverage")).json()
    assert cover["free_available"] is False and cover["plans"] == []
    # on a paid plan, more workspaces join it up to what it covers (Single covers 3)
    await workspaces.update_one({"id": first}, {"$set": {"plan": "single"}})
    from app.shared import entitlements

    entitlements.forget()
    cover = (await api_client.get("/api/v1/billing/coverage")).json()
    assert cover["plans"][0]["allowed"] == 3 and cover["free_available"] is True
    for n in range(2):
        ok = await api_client.post("/api/v1/workspaces/", json={"name": f"Client {n}", "tier": "single", "billing_group_id": first})
        assert ok.status_code == 201, ok.text
    full = await api_client.post("/api/v1/workspaces/", json={"name": "One more", "tier": "single", "billing_group_id": first})
    assert full.status_code == 402
