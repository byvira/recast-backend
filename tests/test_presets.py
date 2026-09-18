"""Tests for /api/v1/presets — real backend for the Presets page.

The Presets page was 100% frontend mock state (INITIAL_PRESETS) with no
backend model, no persistence, and no delete action at all. This is the
real CRUD backing it.
"""

from tests.conftest import create_workspace, invite_and_accept, signup_new_user


def _valid_body(**overrides) -> dict:
    body = {
        "title": "5-Part Executive Contrast Framework",
        "category": "text_thread",
        "category_label": "X & Threads",
        "description": "Contrast-driven thread structure for executive takes.",
        "target_channels": ["twitter", "threads"],
        "structure_rules": [
            {"step_index": 1, "section_name": "Hook Thesis", "char_limit": 240, "guidelines": "Deliver clear main takeaway."},
        ],
        "default_hashtags": ["#Growth", "#Systems"],
        "hook_formula_example": "Deliver clear main takeaway.",
    }
    body.update(overrides)
    return body


async def test_create_and_list_preset(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/presets/", json=_valid_body())
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["title"] == "5-Part Executive Contrast Framework"
    assert body["version"] == 1
    assert body["usage_count"] == 0

    res = await api_client.get("/api/v1/presets/")
    assert res.status_code == 200
    titles = [p["title"] for p in res.json()]
    assert "5-Part Executive Contrast Framework" in titles


async def test_get_preset_by_id(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/presets/", json=_valid_body())
    preset_id = res.json()["id"]

    res = await api_client.get(f"/api/v1/presets/{preset_id}")
    assert res.status_code == 200
    assert res.json()["id"] == preset_id


async def test_get_preset_404_for_nonexistent(api_client):
    await signup_new_user(api_client)

    res = await api_client.get("/api/v1/presets/does-not-exist")
    assert res.status_code == 404


async def test_update_preset_bumps_version(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/presets/", json=_valid_body())
    preset_id = res.json()["id"]

    res = await api_client.patch(f"/api/v1/presets/{preset_id}", json={"title": "Renamed"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["title"] == "Renamed"
    assert body["version"] == 2
    # Untouched fields survive.
    assert body["category"] == "text_thread"


async def test_update_preset_400_when_nothing_provided(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/presets/", json=_valid_body())
    preset_id = res.json()["id"]

    res = await api_client.patch(f"/api/v1/presets/{preset_id}", json={})
    assert res.status_code == 400


async def test_delete_preset_is_soft_and_idempotent_read(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/presets/", json=_valid_body())
    preset_id = res.json()["id"]

    res = await api_client.delete(f"/api/v1/presets/{preset_id}")
    assert res.status_code == 204

    res = await api_client.get(f"/api/v1/presets/{preset_id}")
    assert res.status_code == 404

    res = await api_client.get("/api/v1/presets/")
    assert preset_id not in [p["id"] for p in res.json()]


async def test_delete_preset_404_for_already_deleted(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/presets/", json=_valid_body())
    preset_id = res.json()["id"]
    await api_client.delete(f"/api/v1/presets/{preset_id}")

    res = await api_client.delete(f"/api/v1/presets/{preset_id}")
    assert res.status_code == 404


async def test_presets_are_scoped_to_workspace(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/presets/", json=_valid_body())
    preset_id = res.json()["id"]

    ws_id = await create_workspace(api_client, "Other Workspace", tier="duo")
    res = await api_client.get("/api/v1/presets/", headers={"X-Workspace-Id": ws_id})
    assert preset_id not in [p["id"] for p in res.json()]

    res = await api_client.get(f"/api/v1/presets/{preset_id}", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 404


async def test_create_preset_requires_create_content_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Preset Perms", tier="large")
    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/presets/", json=_valid_body(), headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_delete_preset_requires_edit_content_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Preset Perms", tier="large")
    res = await api_client.post("/api/v1/presets/", json=_valid_body(), headers={"X-Workspace-Id": ws_id})
    preset_id = res.json()["id"]

    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")
    res = await viewer_client.delete(f"/api/v1/presets/{preset_id}", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 403
