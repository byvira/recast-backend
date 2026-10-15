"""Retention for what B20 to B22 added: old webhook deliveries, long-ended review links with their comments, and the daily file retry."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import get_db
from app.video import retention


async def test_old_finished_webhook_deliveries_are_removed_but_waiting_and_recent_ones_stay():
    db = get_db()
    ws = f"ws-{uuid4()}"
    now = datetime.now(timezone.utc)
    old, fresh = now - timedelta(days=45), now - timedelta(days=2)
    rows = [("old-ok", "delivered", old), ("old-failed", "failed", old), ("old-retrying", "retrying", old), ("fresh-ok", "delivered", fresh)]
    for rid, status, when in rows:
        await db["video_webhook_deliveries"].insert_one({"id": rid + ws, "workspace_id": ws, "status": status, "created_at": when})

    out = await retention.trim_records(now)

    left = {d["id"] async for d in db["video_webhook_deliveries"].find({"workspace_id": ws})}
    assert left == {"old-retrying" + ws, "fresh-ok" + ws} and out["webhook_deliveries"] >= 2


async def test_review_links_ended_or_expired_for_ninety_days_go_with_their_comments_and_answers():
    db = get_db()
    ws = f"ws-{uuid4()}"
    now = datetime.now(timezone.utc)
    await db["video_review_links"].insert_many([
        {"id": "gone" + ws, "token": "a" + ws, "workspace_id": ws, "revoked": True, "revoked_at": now - timedelta(days=100), "expires_at": now + timedelta(days=5)},
        {"id": "lapsed" + ws, "token": "b" + ws, "workspace_id": ws, "revoked": False, "expires_at": now - timedelta(days=120)},
        {"id": "recent" + ws, "token": "c" + ws, "workspace_id": ws, "revoked": True, "revoked_at": now - timedelta(days=10), "expires_at": now + timedelta(days=5)},
        {"id": "live" + ws, "token": "d" + ws, "workspace_id": ws, "revoked": False, "expires_at": now + timedelta(days=5)},
    ])
    for lid in ("gone", "recent"):
        await db["video_review_comments"].insert_one({"id": "c-" + lid + ws, "link_id": lid + ws, "workspace_id": ws})
        await db["video_review_decisions"].insert_one({"id": "d-" + lid + ws, "link_id": lid + ws, "workspace_id": ws})

    await retention.trim_records(now)

    links = {lk["id"] async for lk in db["video_review_links"].find({"workspace_id": ws})}
    assert links == {"recent" + ws, "live" + ws}
    assert {c["id"] async for c in db["video_review_comments"].find({"workspace_id": ws})} == {"c-recent" + ws}
    assert {c["id"] async for c in db["video_review_decisions"].find({"workspace_id": ws})} == {"d-recent" + ws}


async def test_the_daily_job_also_retries_files_storage_could_not_remove(monkeypatch):
    from app.video import deletion

    seen = []

    async def fake_retry(limit=100):
        seen.append(limit)
        return 3

    monkeypatch.setattr(deletion, "retry_failed_files", fake_retry)
    out = await retention.run_video_retention.__wrapped__() if hasattr(retention.run_video_retention, "__wrapped__") else await retention.run_video_retention()
    assert seen and out.get("files_retried") == 3
