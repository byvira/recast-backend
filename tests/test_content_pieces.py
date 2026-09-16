"""Tests for PATCH /api/v1/content/pieces/{id}/approve — Module 2 Stage 3.

This endpoint was fully real already (a genuine Mongo write via
update_piece_status), but had zero test coverage of any kind — the whole
app/api/v1/content.py router did not appear in a single test file before
this. The bug this module fixes was entirely on the frontend side (the
live Approve button never called this endpoint at all); these tests
cover the backend contract that button now actually depends on.

Pieces are seeded directly via storage.ensure_session_exists/
save_live_piece rather than through a real generation run — no LLM call
needed to test an approve endpoint, consistent with this suite's rule of
never calling a real LLM.
"""

from uuid import uuid4

from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace, invite_and_accept


async def _seed_piece(workspace_id: str, user_id: str, brand_id: str | None = None) -> str:
    session_id = str(uuid4())
    brand_id = brand_id or str(uuid4())
    await ensure_session_exists(
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        brand_id=brand_id,
        source_type="text",
    )
    return await save_live_piece(
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        brand_id=brand_id,
        platform="LinkedIn",
        content="A real piece waiting for approval.",
        word_count=6,
        char_count=35,
    )


async def test_approve_piece_persists_for_real(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Approve WS")
    piece_id = await _seed_piece(ws_id, profile["id"])

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/approve",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    assert res.json()["approval_status"] == "approved"

    # Independently re-fetch — confirms the write actually persisted,
    # not just that the response claimed success.
    refetch = await client.get(
        f"/api/v1/content/pieces/{piece_id}",
        headers={"X-Workspace-Id": ws_id},
    )
    assert refetch.status_code == 200
    assert refetch.json()["approval_status"] == "approved"


async def test_approve_piece_404_for_nonexistent_piece(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Approve WS")

    res = await client.patch(
        f"/api/v1/content/pieces/{uuid4()}/approve",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 404


async def test_approve_piece_404_for_wrong_workspace(signup_user):
    owner_client, owner_profile = await signup_user()
    ws_a = await create_workspace(owner_client, "WS A")
    piece_id = await _seed_piece(ws_a, owner_profile["id"])

    other_client, _ = await signup_user()
    ws_b = await create_workspace(other_client, "WS B")

    res = await other_client.patch(
        f"/api/v1/content/pieces/{piece_id}/approve",
        headers={"X-Workspace-Id": ws_b},
    )
    assert res.status_code == 404


async def test_approve_piece_blocked_for_editor(signup_user, make_client):
    owner_client, owner_profile = await signup_user()
    ws_id = await create_workspace(owner_client, "Approve RBAC WS")
    piece_id = await _seed_piece(ws_id, owner_profile["id"])

    editor_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "editor")

    res = await editor_client.patch(
        f"/api/v1/content/pieces/{piece_id}/approve",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_approve_piece_blocked_for_viewer(signup_user, make_client):
    owner_client, owner_profile = await signup_user()
    ws_id = await create_workspace(owner_client, "Approve RBAC WS")
    piece_id = await _seed_piece(ws_id, owner_profile["id"])

    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")

    res = await viewer_client.patch(
        f"/api/v1/content/pieces/{piece_id}/approve",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_approve_piece_allowed_for_admin(signup_user, make_client):
    owner_client, owner_profile = await signup_user()
    ws_id = await create_workspace(owner_client, "Approve RBAC WS")
    piece_id = await _seed_piece(ws_id, owner_profile["id"])

    admin_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "admin")

    res = await admin_client.patch(
        f"/api/v1/content/pieces/{piece_id}/approve",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200
    assert res.json()["approval_status"] == "approved"
