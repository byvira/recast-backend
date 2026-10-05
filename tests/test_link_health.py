"""A published post that is gone from its platform is noticed, told to the member once, and no longer checked."""
import httpx

from app.db.mongo import activity_entries, content_pieces
from app.pipelines.analytics import link_health
from app.pipelines.analytics.base import classify_failure, failure_from_status


def _error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/post")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, text=body, request=request))


def test_a_gone_post_is_told_apart_from_a_refused_connection_and_a_passing_error():
    assert classify_failure(_error(404)) == "not_found"
    assert classify_failure(_error(400, '{"error":{"message":"Object with ID does not exist","code":100,"error_subcode":33}}')) == "not_found"
    assert classify_failure(_error(400, "RecordNotFound: Could not locate record")) == "not_found"
    assert classify_failure(_error(401)) == "auth_error"
    assert classify_failure(_error(400, '{"error":{"code":190,"message":"Error validating access token"}}')) == "auth_error"
    assert classify_failure(_error(500)) == "transient"
    assert classify_failure(TimeoutError("slow")) == "transient"
    assert failure_from_status(404, 404) == "not_found"
    assert failure_from_status(404, 500) == "transient"
    assert failure_from_status(401, 404) == "auth_error"


async def _piece(workspace="w-lh", piece="p-lh"):
    await content_pieces.delete_many({"workspace_id": workspace})
    await activity_entries.delete_many({"workspace_id": workspace})
    await content_pieces.insert_one({
        "workspace_id": workspace, "piece_id": piece, "platform": "LinkedIn", "publish_status": "published",
        "platform_post_id": "urn:li:share:1", "created_by": "u-lh",
    })
    return {"piece_id": piece, "platform": "linkedin", "platform_post_id": "urn:li:share:1"}


async def test_a_post_is_removed_only_after_two_gone_answers_in_a_row_and_the_member_is_told_once():
    post = await _piece()
    try:
        assert await link_health.note_unreadable("w-lh", post, "not_found") is None
        assert (await content_pieces.find_one({"piece_id": "p-lh"}))["platform_missing_checks"] == 1
        assert await link_health.note_unreadable("w-lh", post, "not_found") == "removed"
        row = await content_pieces.find_one({"piece_id": "p-lh"})
        assert row["platform_state"] == "removed" and row["platform_removed_at"]
        assert await activity_entries.count_documents({"_id": "system:post-removed:p-lh"}) == 1
        # Already removed: further answers change nothing.
        assert await link_health.note_unreadable("w-lh", post, "not_found") is None
    finally:
        await content_pieces.delete_many({"workspace_id": "w-lh"})
        await activity_entries.delete_many({"workspace_id": "w-lh"})


async def test_a_post_that_shows_up_again_between_checks_is_live_and_the_count_starts_over():
    post = await _piece()
    try:
        await link_health.note_unreadable("w-lh", post, "not_found")
        await link_health.note_readable("w-lh", "p-lh")
        row = await content_pieces.find_one({"piece_id": "p-lh"})
        assert row["platform_state"] == "live" and row["platform_missing_checks"] == 0
        assert await link_health.note_unreadable("w-lh", post, "not_found") is None
    finally:
        await content_pieces.delete_many({"workspace_id": "w-lh"})


async def test_a_refused_connection_marks_the_post_unreachable_not_removed_and_passing_errors_change_nothing():
    post = await _piece()
    try:
        assert await link_health.note_unreadable("w-lh", post, "transient") is None
        assert "platform_state" not in await content_pieces.find_one({"piece_id": "p-lh"})
        assert await link_health.note_unreadable("w-lh", post, "auth_error") == "unreachable"
        assert (await content_pieces.find_one({"piece_id": "p-lh"}))["platform_state"] == "unreachable"
    finally:
        await content_pieces.delete_many({"workspace_id": "w-lh"})


class _Gone:
    """A platform reader that says the post is gone."""

    async def fetch_post_metrics(self, **kwargs):
        from app.pipelines.analytics.base import PostMetrics

        return PostMetrics(platform="linkedin", post_id=kwargs["piece_id"], platform_post_id=kwargs["platform_post_id"],
                           fetch_ok=False, failure="not_found")


class _Here:
    async def fetch_post_metrics(self, **kwargs):
        from app.pipelines.analytics.base import PostMetrics

        return PostMetrics(platform="linkedin", post_id=kwargs["piece_id"], platform_post_id=kwargs["platform_post_id"], likes=3)


def _readers(monkeypatch, reader):
    from app.pipelines.analytics import aggregator

    async def token(workspace_id, platform):
        return {"access_token": "t"}

    monkeypatch.setattr(aggregator, "_get_fetcher", lambda platform: reader)
    monkeypatch.setattr(aggregator, "get_token", token)


async def test_checking_now_finds_a_removed_post_on_the_second_gone_answer(monkeypatch):
    from datetime import datetime, timezone

    post = await _piece()
    piece = await content_pieces.find_one({"piece_id": "p-lh"})
    _readers(monkeypatch, _Gone())
    try:
        first = await link_health.check_now("w-lh", piece)
        assert first["state"] in ("unknown", "live") and first["state"] != "removed"
        second = await link_health.check_now("w-lh", piece)
        assert second["state"] == "removed" and second["checked_at"]
        assert post["piece_id"] == "p-lh"
    finally:
        await content_pieces.delete_many({"workspace_id": "w-lh"})
        await activity_entries.delete_many({"workspace_id": "w-lh"})


async def test_checking_now_marks_a_post_that_is_there_as_live(monkeypatch):
    await _piece()
    piece = await content_pieces.find_one({"piece_id": "p-lh"})
    _readers(monkeypatch, _Here())
    try:
        result = await link_health.check_now("w-lh", piece)
        assert result["state"] == "live" and result["checked_at"]
    finally:
        await content_pieces.delete_many({"workspace_id": "w-lh"})


async def test_the_daily_job_asks_about_old_posts_not_asked_for_a_week_and_skips_removed_ones(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.workers.link_checks import check_old_post_links

    await content_pieces.delete_many({"workspace_id": {"$in": ["w-old", "w-old2"]}})
    long_ago = datetime.now(timezone.utc) - timedelta(days=200)
    base = {"publish_status": "published", "platform": "LinkedIn", "platform_post_id": "urn:1", "published_at": long_ago}
    await content_pieces.insert_many([
        {**base, "workspace_id": "w-old", "piece_id": "old-1"},
        {**base, "workspace_id": "w-old", "piece_id": "old-2", "platform_state": "removed"},
        {**base, "workspace_id": "w-old2", "piece_id": "old-3", "platform_checked_at": datetime.now(timezone.utc)},
        {**base, "workspace_id": "w-old2", "piece_id": "new-4", "published_at": datetime.now(timezone.utc)},
    ])
    asked = []

    async def fake_check(workspace_id, piece):
        asked.append(piece["piece_id"])
        return {"state": "live", "checked_at": None}

    monkeypatch.setattr(link_health, "check_now", fake_check)
    try:
        await check_old_post_links()
        assert "old-1" in asked
        assert not {"old-2", "old-3", "new-4"} & set(asked)
    finally:
        await content_pieces.delete_many({"workspace_id": {"$in": ["w-old", "w-old2"]}})


async def test_the_check_link_route_needs_a_published_post_and_returns_the_state(api_client, monkeypatch):
    from tests.conftest import create_workspace, signup_new_user

    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Link WS")
    headers = {"X-Workspace-Id": ws}
    await content_pieces.insert_many([
        {"workspace_id": ws, "piece_id": "draft-1", "platform": "LinkedIn", "publish_status": "pending", "content": "x"},
        {"workspace_id": ws, "piece_id": "live-1", "platform": "LinkedIn", "publish_status": "published",
         "platform_post_id": "urn:li:share:9", "content": "x"},
    ])
    _readers(monkeypatch, _Here())
    try:
        assert (await api_client.post("/api/v1/content/pieces/draft-1/check-link", headers=headers)).status_code == 400
        assert (await api_client.post("/api/v1/content/pieces/missing/check-link", headers=headers)).status_code == 404
        res = await api_client.post("/api/v1/content/pieces/live-1/check-link", headers=headers)
        assert res.status_code == 200, res.text
        assert res.json()["state"] == "live" and res.json()["piece_id"] == "live-1"
    finally:
        await content_pieces.delete_many({"workspace_id": ws})
