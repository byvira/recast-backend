"""Tests for the Drafts/Library kanban stage — Module 2 Stage 8.

The mock Drafts UI has a 4-step kanban (drafting -> staging -> scheduled ->
published, plus a lateral archive) that no real DB field stores. These tests
verify the derived `stage` on GET /content/pieces matches the real
approval_status/publish_status/archived fields correctly, that the
stage filter paginates over the right set, and that /pieces/{id}/archive
works and is workspace-scoped.
"""

from uuid import uuid4

from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace


async def _seed_piece(workspace_id: str, user_id: str, brand_id: str, platform: str = "LinkedIn") -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    return await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform=platform,
        content=f"Content for {platform}.", word_count=3, char_count=20,
    )


async def test_new_piece_stage_is_drafting(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Stage WS")
    await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.get("/api/v1/content/pieces", headers={"X-Workspace-Id": ws_id})
    assert res.json()["items"][0]["stage"] == "drafting"


async def test_approved_piece_stage_is_staging(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Stage WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200
    assert res.json()["stage"] == "staging"


async def test_scheduled_piece_stage_is_scheduled(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Stage WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))
    await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id})

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": "2026-12-01T10:00:00Z"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200
    assert res.json()["stage"] == "scheduled"


async def test_rejected_piece_falls_back_to_drafting(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Stage WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/reject", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200
    assert res.json()["stage"] == "drafting"


async def test_archive_and_unarchive(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Archive WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/archive",
        json={"archived": True},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200
    assert res.json()["stage"] == "archived"

    res2 = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/archive",
        json={"archived": False},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res2.status_code == 200
    assert res2.json()["stage"] == "drafting"


async def test_archived_takes_priority_over_approved(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Archive Priority WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))
    await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id})

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/archive",
        json={"archived": True},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.json()["stage"] == "archived"


async def test_archive_requires_edit_content_permission(signup_user, make_client):
    from tests.conftest import invite_and_accept

    owner_client, owner_profile = await signup_user()
    ws_id = await create_workspace(owner_client, "Archive RBAC WS")
    piece_id = await _seed_piece(ws_id, owner_profile["id"], str(uuid4()))

    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")
    res = await viewer_client.patch(
        f"/api/v1/content/pieces/{piece_id}/archive",
        json={"archived": True},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_stage_filter_returns_only_matching_pieces(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Stage Filter WS")
    brand_id = str(uuid4())

    drafting_id = await _seed_piece(ws_id, profile["id"], brand_id, platform="LinkedIn")
    staged_id = await _seed_piece(ws_id, profile["id"], brand_id, platform="Instagram")
    await client.patch(f"/api/v1/content/pieces/{staged_id}/approve", headers={"X-Workspace-Id": ws_id})

    res = await client.get(
        "/api/v1/content/pieces?stage=drafting", headers={"X-Workspace-Id": ws_id},
    )
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["piece_id"] == drafting_id

    res2 = await client.get(
        "/api/v1/content/pieces?stage=staging", headers={"X-Workspace-Id": ws_id},
    )
    body2 = res2.json()
    assert body2["total"] == 1
    assert body2["items"][0]["piece_id"] == staged_id


async def test_list_pieces_includes_author_and_brand_name(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Names WS")
    await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.get("/api/v1/content/pieces", headers={"X-Workspace-Id": ws_id})
    item = res.json()["items"][0]
    assert "author_name" in item
    assert "brand_name" in item
    assert item["author_name"] != ""
