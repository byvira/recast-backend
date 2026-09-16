"""Tests for GET /api/v1/content/pieces — Module 2 Stage 8's foundation.

Drafts and Library were both 100% hardcoded mock arrays with zero API
wiring of any kind. There was also no backend endpoint for either page to
even call if it wanted to — only /sessions (session-then-pieces, grouped)
and /pieces/{id} (one piece) existed, nothing that returns a flat,
paginated piece list across a workspace. This is that endpoint.
"""

from uuid import uuid4

from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace, invite_and_accept


async def _seed_piece(
    workspace_id: str, user_id: str, brand_id: str, platform: str = "LinkedIn",
    approval_status_after: str | None = None,
) -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform=platform,
        content=f"Content for {platform}.", word_count=3, char_count=20,
    )
    return piece_id


async def test_list_pieces_returns_real_pieces_most_recent_first(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "List Pieces WS")
    brand_id = str(uuid4())

    first = await _seed_piece(ws_id, profile["id"], brand_id, platform="LinkedIn")
    second = await _seed_piece(ws_id, profile["id"], brand_id, platform="Instagram")

    res = await client.get("/api/v1/content/pieces", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 2
    ids = [p["piece_id"] for p in body["items"]]
    # Most recent (second) first.
    assert ids == [second, first]


async def test_list_pieces_is_workspace_scoped(signup_user):
    client_a, profile_a = await signup_user()
    ws_a = await create_workspace(client_a, "WS A")
    await _seed_piece(ws_a, profile_a["id"], str(uuid4()))

    client_b, _ = await signup_user()
    ws_b = await create_workspace(client_b, "WS B")

    res = await client_b.get("/api/v1/content/pieces", headers={"X-Workspace-Id": ws_b})
    assert res.status_code == 200
    assert res.json()["total"] == 0


async def test_list_pieces_filters_by_platform(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Filter WS")
    brand_id = str(uuid4())
    await _seed_piece(ws_id, profile["id"], brand_id, platform="LinkedIn")
    await _seed_piece(ws_id, profile["id"], brand_id, platform="Instagram")

    res = await client.get(
        "/api/v1/content/pieces?platform=Instagram", headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["platform"] == "Instagram"


async def test_list_pieces_filters_by_approval_status(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Filter WS")
    brand_id = str(uuid4())
    approved_piece = await _seed_piece(ws_id, profile["id"], brand_id)
    await _seed_piece(ws_id, profile["id"], brand_id)

    approve_res = await client.patch(
        f"/api/v1/content/pieces/{approved_piece}/approve",
        headers={"X-Workspace-Id": ws_id},
    )
    assert approve_res.status_code == 200

    res = await client.get(
        "/api/v1/content/pieces?approval_status=approved", headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["piece_id"] == approved_piece


async def test_list_pieces_excludes_deleted(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Delete WS")
    brand_id = str(uuid4())
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)

    del_res = await client.delete(
        f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id},
    )
    assert del_res.status_code == 200

    res = await client.get("/api/v1/content/pieces", headers={"X-Workspace-Id": ws_id})
    assert res.json()["total"] == 0


async def test_list_pieces_paginates(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Paginate WS")
    brand_id = str(uuid4())
    for _ in range(3):
        await _seed_piece(ws_id, profile["id"], brand_id)

    res = await client.get(
        "/api/v1/content/pieces?page=1&limit=2", headers={"X-Workspace-Id": ws_id},
    )
    body = res.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["has_more"] is True

    res2 = await client.get(
        "/api/v1/content/pieces?page=2&limit=2", headers={"X-Workspace-Id": ws_id},
    )
    body2 = res2.json()
    assert len(body2["items"]) == 1
    assert body2["has_more"] is False


async def test_list_pieces_readable_by_viewer(signup_user, make_client):
    owner_client, owner_profile = await signup_user()
    ws_id = await create_workspace(owner_client, "Viewer Read WS")
    await _seed_piece(ws_id, owner_profile["id"], str(uuid4()))

    viewer_client, _ = await invite_and_accept(owner_client, make_client, ws_id, "viewer")

    res = await viewer_client.get("/api/v1/content/pieces", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200
    assert res.json()["total"] == 1
