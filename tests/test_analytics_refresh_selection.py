"""The six-hour analytics refresh picks the right posts, in the right order, and records when each was last read."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import content_pieces, get_db
from app.pipelines.analytics import scheduler
from app.pipelines.analytics.base import PostMetrics

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _published(ws_id: str, name: str, *, published_days_ago: float, fetched_hours_ago: float | None, **extra) -> str:
    piece_id = f"{name}-{uuid4()}"
    doc = {
        "piece_id": piece_id, "workspace_id": ws_id, "platform": "LinkedIn", "publish_status": "published",
        "platform_post_id": f"post-{name}", "published_at": NOW() - timedelta(days=published_days_ago), "deleted": False, **extra,
    }
    if fetched_hours_ago is not None:
        doc["metrics_fetched_at"] = NOW() - timedelta(hours=fetched_hours_ago)
    await content_pieces.insert_one(doc)
    return piece_id


def _patch(monkeypatch, returned=None):
    asked: list[list[dict]] = []

    async def _accounts(**kwargs):
        return []

    async def _posts(*, workspace_id, posts):
        asked.append(list(posts))
        return returned(posts) if returned else []

    async def _snapshot(workspace_id):
        return None

    monkeypatch.setattr(scheduler, "fetch_account_metrics_all", _accounts)
    monkeypatch.setattr(scheduler, "fetch_post_metrics_all", _posts)
    monkeypatch.setattr(scheduler, "record_daily_snapshot", _snapshot)
    return asked


async def test_never_read_posts_come_first_then_the_least_recently_read(monkeypatch):
    ws_id = f"ws-{uuid4()}"
    older_read = await _published(ws_id, "older", published_days_ago=3, fetched_hours_ago=30)
    never = await _published(ws_id, "never", published_days_ago=1, fetched_hours_ago=None)
    newer_read = await _published(ws_id, "newer", published_days_ago=2, fetched_hours_ago=8)
    asked = _patch(monkeypatch)

    await scheduler._refresh_workspace_analytics(get_db(), ws_id)

    assert [p["piece_id"] for p in asked[0]] == [never, older_read, newer_read]
    assert all(p["platform"] == "linkedin" for p in asked[0])           # the registry key, not the display text
    assert {p["platform_post_id"] for p in asked[0]} == {"post-older", "post-never", "post-newer"}


async def test_posts_read_recently_or_settled_or_removed_are_left_out(monkeypatch):
    ws_id = f"ws-{uuid4()}"
    due = await _published(ws_id, "due", published_days_ago=5, fetched_hours_ago=12)
    await _published(ws_id, "fresh", published_days_ago=5, fetched_hours_ago=1)           # read an hour ago
    await _published(ws_id, "settled", published_days_ago=120, fetched_hours_ago=None)    # older than 90 days
    await _published(ws_id, "gone", published_days_ago=2, fetched_hours_ago=None, platform_state="removed")
    await content_pieces.insert_one({
        "piece_id": f"noid-{uuid4()}", "workspace_id": ws_id, "platform": "LinkedIn", "publish_status": "published",
        "platform_post_id": None, "published_at": NOW(), "deleted": False,
    })
    asked = _patch(monkeypatch)

    await scheduler._refresh_workspace_analytics(get_db(), ws_id)

    assert [p["piece_id"] for p in asked[0]] == [due]


async def test_the_read_time_is_written_to_the_post_by_its_own_id(monkeypatch):
    ws_id = f"ws-{uuid4()}"
    piece_id = await _published(ws_id, "one", published_days_ago=1, fetched_hours_ago=None)
    other = await _published(ws_id, "two", published_days_ago=1, fetched_hours_ago=1)

    def _metrics(posts):
        return [PostMetrics(workspace_id=ws_id, platform="linkedin", post_id=p["piece_id"], platform_post_id=p["platform_post_id"], likes=3)
                for p in posts]

    _patch(monkeypatch, returned=_metrics)
    before = NOW()

    await scheduler._refresh_workspace_analytics(get_db(), ws_id)

    stored = await content_pieces.find_one({"piece_id": piece_id})
    assert _utc(stored["metrics_fetched_at"]) >= before
    skipped = await content_pieces.find_one({"piece_id": other})
    assert _utc(skipped["metrics_fetched_at"]) < before
    assert await get_db()["post_metrics"].count_documents({"workspace_id": ws_id, "post_id": piece_id}) == 1
