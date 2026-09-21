"""Tests for the scheduled-posts worker's retry/requeue behavior (R2-7).

Previously a single failure permanently marked a scheduled piece "failed" —
no retry, no requeue, regardless of whether the error was transient (a
platform's temporary 503) or permanent (revoked token). Mirrors
/publish/now's existing classify_error/should_retry/get_retry_delay usage
(app/api/v1/publish.py), but requeues instead of sleeping in-request, since
this runs from a background worker tick, not an HTTP request.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.db.mongo import content_pieces
from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.token_store import save_token
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from app.workers.scheduled_posts import _publish_scheduled_piece
from tests.conftest import create_workspace


async def _seed_due_piece(workspace_id: str, user_id: str, brand_id: str) -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform="LinkedIn",
        content="Due scheduled post.", word_count=4, char_count=20,
    )
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await content_pieces.update_one(
        {"piece_id": piece_id},
        {"$set": {
            "publish_status": "queued",
            "publish_target": "linkedin",
            "publish_scheduled_at": due,
        }},
    )
    return piece_id


async def _connect_linkedin(workspace_id: str) -> None:
    await save_token(
        workspace_id=workspace_id, platform="linkedin",
        access_token="fake-access-token", refresh_token=None,
        expires_at=None, platform_user_id="urn:li:person:test",
        username="test-user", connected_by="",
    )


async def test_transient_failure_requeues_instead_of_failing(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Retry WS")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_due_piece(ws_id, profile["id"], str(uuid4()))
    piece = await content_pieces.find_one({"piece_id": piece_id})

    fake_publisher = AsyncMock()
    fake_publisher.publish = AsyncMock(return_value=PublishResult(
        success=False, platform="linkedin", piece_id=piece_id,
        error_code=503, error_message="Service temporarily unavailable",
    ))

    with patch("app.workers.scheduled_posts.get_publisher", return_value=fake_publisher):
        await _publish_scheduled_piece(piece)

    updated = await content_pieces.find_one({"piece_id": piece_id})
    assert updated["publish_status"] == "queued"  # not "failed"
    assert updated["publish_attempts"] == 1
    assert updated["publish_scheduled_at"] > piece["publish_scheduled_at"]  # pushed into the future


async def test_auth_failure_fails_immediately_no_retry(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Retry WS 2")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_due_piece(ws_id, profile["id"], str(uuid4()))
    piece = await content_pieces.find_one({"piece_id": piece_id})

    fake_publisher = AsyncMock()
    fake_publisher.publish = AsyncMock(return_value=PublishResult(
        success=False, platform="linkedin", piece_id=piece_id,
        error_code=401, error_message="Token expired",
    ))

    with patch("app.workers.scheduled_posts.get_publisher", return_value=fake_publisher):
        await _publish_scheduled_piece(piece)

    updated = await content_pieces.find_one({"piece_id": piece_id})
    assert updated["publish_status"] == "failed"
    assert updated.get("publish_attempts", 0) == 0  # never incremented — no retry attempted


async def test_transient_failure_fails_after_max_retries(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Retry WS 3")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_due_piece(ws_id, profile["id"], str(uuid4()))

    fake_publisher = AsyncMock()
    fake_publisher.publish = AsyncMock(return_value=PublishResult(
        success=False, platform="linkedin", piece_id=piece_id,
        error_code=503, error_message="Service temporarily unavailable",
    ))

    # MAX_RETRIES[TRANSIENT] == 3 — run one more attempt than that.
    with patch("app.workers.scheduled_posts.get_publisher", return_value=fake_publisher):
        for _ in range(4):
            piece = await content_pieces.find_one({"piece_id": piece_id})
            await _publish_scheduled_piece(piece)

    updated = await content_pieces.find_one({"piece_id": piece_id})
    assert updated["publish_status"] == "failed"
    assert updated["publish_attempts"] == 3
