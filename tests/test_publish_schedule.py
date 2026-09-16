"""Tests for Module 2 Stage 9 — real scheduling and publishing.

Three write paths used to disagree about what "scheduled" meant:
app.api.v1.publish's now-removed /schedule wrote publish_status="queued"
(the value the real worker, app.workers.scheduled_posts, actually polls
for); app.api.v1.content's /pieces/{id}/schedule wrote "scheduled"; and
generation-time schedule_mode also wrote "scheduled". Pieces scheduled
through the latter two looked scheduled in the UI forever but the worker
never picked them up. These tests cover the fixed, single real path:
content.py's /pieces/{id}/schedule now validates a connected token +
platform support + content, writes "queued", and a new
/pieces/{id}/cancel-schedule reverses it. Also covers /publish/now (mocked
publisher, no real LinkedIn/Instagram/Facebook API calls) and the derived
kanban "scheduled"/"failed" stages.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.token_store import save_token
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
        content=f"Real post content for {platform}.", word_count=5, char_count=40,
    )


async def _connect_linkedin(workspace_id: str) -> None:
    await save_token(
        workspace_id=workspace_id, platform="linkedin",
        access_token="fake-access-token", refresh_token=None,
        expires_at=None, platform_user_id="urn:li:person:test",
        username="test-user", connected_by="",
    )


async def test_schedule_without_connected_token_rejected(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Schedule WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": "2026-12-01T10:00:00Z"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400
    assert "not connected" in res.json()["detail"].lower()


async def test_schedule_with_connected_token_writes_queued_not_scheduled(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Schedule WS")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": "2026-12-01T10:00:00Z"},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["publish_status"] == "queued"
    assert body["publish_target"] == "linkedin"
    assert body["stage"] == "scheduled"


async def test_schedule_unsupported_platform_rejected(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Schedule WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="Blog")

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": "2026-12-01T10:00:00Z"},
        headers={"X-Workspace-Id": ws_id},
    )
    # No publisher exists for "Blog" — connection check fails first (no
    # token either), but either way this must never silently queue.
    assert res.status_code == 400


async def test_cancel_schedule_reverts_to_pending(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Cancel WS")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": "2026-12-01T10:00:00Z"},
        headers={"X-Workspace-Id": ws_id},
    )

    res = await client.post(
        f"/api/v1/content/pieces/{piece_id}/cancel-schedule",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["publish_status"] == "pending"
    assert body["stage"] == "drafting"


async def test_cancel_schedule_on_non_queued_piece_rejected(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Cancel WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.post(
        f"/api/v1/content/pieces/{piece_id}/cancel-schedule",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400


async def test_publish_now_success_marks_published(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Publish WS")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    fake_publisher = AsyncMock()
    fake_publisher.publish = AsyncMock(return_value=PublishResult(
        success=True, platform="linkedin", piece_id=piece_id,
        platform_post_id="post-123", platform_post_url="https://linkedin.com/post-123",
    ))

    with patch("app.api.v1.publish.get_publisher", return_value=fake_publisher):
        res = await client.post(
            "/api/v1/publish/now",
            json={"piece_id": piece_id},
            headers={"X-Workspace-Id": ws_id},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert body["platform"] == "linkedin"

    check = await client.get(f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id})
    assert check.json()["stage"] == "published"


async def test_publish_now_without_token_rejected(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Publish WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))

    res = await client.post(
        "/api/v1/publish/now",
        json={"piece_id": piece_id},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400
    assert "not connected" in res.json()["detail"].lower()


async def test_failed_piece_shows_as_failed_stage_not_staging(signup_user):
    """A publish failure must be visibly distinct, never silently look like
    an ordinary approved-and-waiting piece (no-silent-success)."""
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Failed WS")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))
    await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id})

    fake_publisher = AsyncMock()
    fake_publisher.publish = AsyncMock(return_value=PublishResult(
        success=False, platform="linkedin", piece_id=piece_id,
        error_type="AUTH", error_code=401, error_message="Token expired",
    ))

    with patch("app.api.v1.publish.get_publisher", return_value=fake_publisher):
        res = await client.post(
            "/api/v1/publish/now",
            json={"piece_id": piece_id},
            headers={"X-Workspace-Id": ws_id},
        )
    assert res.status_code == 401

    check = await client.get(f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id})
    body = check.json()
    assert body["publish_status"] == "failed"
    assert body["stage"] == "failed"
