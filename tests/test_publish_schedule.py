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

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.db.mongo import content_pieces
from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.token_store import save_token
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace


# A schedule time safely in the future, whenever the suite runs.
_LATER = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _approve(client, ws_id: str, piece_id: str) -> None:
    """Publishing and scheduling only take approved posts."""
    res = await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text


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
        json={"scheduled_at": _LATER},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400
    assert "not connected" in res.json()["detail"].lower()


async def test_schedule_with_connected_token_writes_queued_not_scheduled(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Schedule WS")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))
    await _approve(client, ws_id, piece_id)

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": _LATER},
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
        json={"scheduled_at": _LATER},
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
    await _approve(client, ws_id, piece_id)

    await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": _LATER},
        headers={"X-Workspace-Id": ws_id},
    )

    res = await client.post(
        f"/api/v1/content/pieces/{piece_id}/cancel-schedule",
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["publish_status"] == "pending"
    # scheduling needs approval, so the cancelled post is still approved: back to waiting to be published, not back to drafting
    assert body["stage"] == "staging"


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
    await _approve(client, ws_id, piece_id)

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
    # 409 with a structured reason, never 401: a 401 makes the frontend's
    # axios interceptor refresh the session and replay the publish request.
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "platform_reconnect_required"
    assert detail["platform"] == "linkedin"
    assert "Reconnect it in Settings" in detail["message"]
    assert "/api/" not in detail["message"]  # no raw API path in user-facing copy

    check = await client.get(f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id})
    body = check.json()
    assert body["publish_status"] == "failed"
    assert body["stage"] == "failed"


async def test_a_dead_platform_token_is_attempted_exactly_once(signup_user):
    """The old 401 made the browser replay the request, so a single click
    caused two real publish attempts. The publisher must be hit once."""
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Once WS")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))
    await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id})

    fake_publisher = AsyncMock()
    fake_publisher.publish = AsyncMock(return_value=PublishResult(
        success=False, platform="linkedin", piece_id=piece_id,
        error_type="AUTH", error_code=401, error_message="Token expired",
    ))

    with patch("app.api.v1.publish.get_publisher", return_value=fake_publisher), \
         patch("app.api.v1.publish.recover_connection", new=AsyncMock(return_value=False)):
        res = await client.post(
            "/api/v1/publish/now", json={"piece_id": piece_id}, headers={"X-Workspace-Id": ws_id},
        )
    assert res.status_code == 409
    assert fake_publisher.publish.await_count == 1


async def test_a_refused_publish_does_not_leave_the_piece_stuck(signup_user):
    """The claim marks the piece "publishing" before the connection check. A
    piece refused for a missing connection used to stay on "publishing", and
    the claim then rejected every later attempt as already in progress."""
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Release WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_status": "failed"}})

    res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 400
    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "failed"  # put back, not left on "publishing"

    # Connecting afterwards makes it publishable again.
    await _connect_linkedin(ws_id)
    await _approve(client, ws_id, piece_id)
    fake_publisher = AsyncMock()
    fake_publisher.publish = AsyncMock(return_value=PublishResult(
        success=True, platform="linkedin", piece_id=piece_id, platform_post_id="p1",
    ))
    with patch("app.api.v1.publish.get_publisher", return_value=fake_publisher):
        again = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers={"X-Workspace-Id": ws_id})
    assert again.status_code == 200, again.text


async def test_a_publisher_crash_does_not_leave_the_piece_publishing(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Crash WS")
    await _connect_linkedin(ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()))
    await _approve(client, ws_id, piece_id)

    fake_publisher = AsyncMock()
    fake_publisher.publish = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.api.v1.publish.get_publisher", return_value=fake_publisher):
        try:
            res = await client.post("/api/v1/publish/now", json={"piece_id": piece_id}, headers={"X-Workspace-Id": ws_id})
            assert res.status_code == 500
        except RuntimeError:
            pass  # the ASGI test client re-raises the app's exception

    doc = await content_pieces.find_one({"piece_id": piece_id})
    assert doc["publish_status"] == "failed"
    assert "try again" in doc["last_error"].lower()


# ── Scheduled YouTube and Instagram need their media ─────────────────────────

async def _connect(workspace_id: str, platform: str) -> None:
    await save_token(
        workspace_id=workspace_id, platform=platform,
        access_token="fake-access-token", refresh_token=None,
        expires_at=None, platform_user_id="acct-1", username="test-user", connected_by="",
    )


_VIDEO = {
    "id": "media-video-1", "workspace_id": "x", "kind": "video", "url": "https://cdn.example/v.mp4",
    "mime_type": "video/mp4", "source": "uploaded", "created_by": "u", "created_at": "2026-09-01T00:00:00Z",
}
_YT_DETAILS = {
    "title": "My reviewed title", "description": "Reviewed description", "tags": ["a", "b"],
    "category_id": "22", "privacy_status": "private", "made_for_kids": False,
}


async def test_scheduling_instagram_without_media_is_refused_up_front(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "IG Schedule WS")
    await _connect(ws_id, "instagram")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="Instagram")

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": _LATER}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400
    assert "image or video" in res.json()["detail"].lower()


async def test_scheduling_youtube_needs_a_video(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "YT Schedule WS")
    await _connect(ws_id, "youtube")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="YouTube")

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": _LATER, "youtube_metadata": _YT_DETAILS},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400
    assert "video" in res.json()["detail"].lower()


async def test_a_scheduled_youtube_upload_keeps_the_details_that_were_reviewed(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "YT Details WS")
    await _connect(ws_id, "youtube")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="YouTube")
    await _approve(client, ws_id, piece_id)
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"media": [{**_VIDEO, "workspace_id": ws_id}]}})

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": _LATER, "youtube_metadata": _YT_DETAILS},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    stored = await content_pieces.find_one({"piece_id": piece_id})
    assert stored["publish_status"] == "queued"
    assert stored["publish_youtube_metadata"]["title"] == "My reviewed title"

    # Cancelling drops them with the schedule.
    cancel = await client.post(
        f"/api/v1/content/pieces/{piece_id}/cancel-schedule", headers={"X-Workspace-Id": ws_id},
    )
    assert cancel.status_code == 200, cancel.text
    after = await content_pieces.find_one({"piece_id": piece_id})
    assert "publish_youtube_metadata" not in after


async def test_a_scheduled_youtube_upload_rejects_invalid_details(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "YT Bad Details WS")
    await _connect(ws_id, "youtube")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="YouTube")
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"media": [{**_VIDEO, "workspace_id": ws_id}]}})

    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/schedule",
        json={"scheduled_at": _LATER, "youtube_metadata": {"title": ""}},
        headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 422


# ── "I posted this myself" for platforms with no publisher ───────────────────

async def _approved_twitter_piece(client, profile, ws_id: str) -> str:
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="Twitter/X")
    approve = await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id})
    assert approve.status_code == 200, approve.text
    return piece_id


async def test_marking_a_manual_platform_post_as_posted_publishes_it(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual Post WS")
    piece_id = await _approved_twitter_piece(client, profile, ws_id)

    res = await client.post(
        f"/api/v1/content/pieces/{piece_id}/mark-posted",
        json={"post_url": "https://x.com/me/status/1"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    stored = await content_pieces.find_one({"piece_id": piece_id})
    assert stored["publish_status"] == "published"
    assert stored["published_manually"] is True
    assert stored["platform_post_url"] == "https://x.com/me/status/1"
    assert stored["published_at"] is not None

    check = await client.get(f"/api/v1/content/pieces/{piece_id}", headers={"X-Workspace-Id": ws_id})
    assert check.json()["stage"] == "published"

    # Doing it twice is refused, not silently repeated.
    again = await client.post(
        f"/api/v1/content/pieces/{piece_id}/mark-posted", json={}, headers={"X-Workspace-Id": ws_id},
    )
    assert again.status_code == 400


async def test_mark_posted_is_refused_for_a_platform_recast_can_publish_to(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual Refused WS")
    piece_id = await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="LinkedIn")
    await client.patch(f"/api/v1/content/pieces/{piece_id}/approve", headers={"X-Workspace-Id": ws_id})

    res = await client.post(
        f"/api/v1/content/pieces/{piece_id}/mark-posted", json={}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 400
    assert "publish now" in res.json()["detail"].lower()
    stored = await content_pieces.find_one({"piece_id": piece_id})
    assert stored["publish_status"] != "published"


async def test_mark_posted_needs_the_post_to_be_in_review_and_a_real_link(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Manual Rules WS")
    draft = await _seed_piece(ws_id, profile["id"], str(uuid4()), platform="Blog")

    not_approved = await client.post(
        f"/api/v1/content/pieces/{draft}/mark-posted", json={}, headers={"X-Workspace-Id": ws_id},
    )
    assert not_approved.status_code == 400
    assert "review" in not_approved.json()["detail"].lower()

    approved = await _approved_twitter_piece(client, profile, ws_id)
    bad_link = await client.post(
        f"/api/v1/content/pieces/{approved}/mark-posted",
        json={"post_url": "javascript:alert(1)"}, headers={"X-Workspace-Id": ws_id},
    )
    assert bad_link.status_code == 400
