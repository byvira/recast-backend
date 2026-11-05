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
    # the workspace is told what was removed, once, in its Activity Log
    note = await db["activity_entries"].find_one({"workspace_id": ws, "category": "workspace_alert"})
    assert note and "2 old review links" in str(note["title"]) and "comments and answers" in note["description"]


async def test_the_daily_job_also_retries_files_storage_could_not_remove(monkeypatch):
    from app.video import deletion

    seen = []

    async def fake_retry(limit=100):
        seen.append(limit)
        return 3

    monkeypatch.setattr(deletion, "retry_failed_files", fake_retry)
    out = await retention.run_video_retention.__wrapped__() if hasattr(retention.run_video_retention, "__wrapped__") else await retention.run_video_retention()
    assert seen and out.get("files_retried") == 3


async def test_unfinished_uploads_are_removed_and_the_workspace_is_told_once(monkeypatch):
    db = get_db()
    ws = f"ws-{uuid4()}"
    now = datetime.now(timezone.utc)
    await db["video_uploads"].insert_many([
        {"id": f"stale-{i}-{ws}", "workspace_id": ws, "status": "started", "created_at": now - timedelta(days=5)} for i in range(3)
    ] + [{"id": f"fresh-{ws}", "workspace_id": ws, "status": "started", "created_at": now - timedelta(hours=3)}])

    assert await retention.sweep_unfinished_uploads(now) == 3
    left = {u["id"] async for u in db["video_uploads"].find({"workspace_id": ws})}
    assert left == {f"fresh-{ws}"}, "a recent upload may still be going and is never touched"
    rows = [r async for r in db["activity_entries"].find({"workspace_id": ws, "category": "workspace_alert"})]
    assert len(rows) == 1 and "3 unfinished uploads" in str(rows[0]["title"])
    # nothing left to remove: no second note, and the same day never writes two
    assert await retention.sweep_unfinished_uploads(now) == 0
    assert await db["activity_entries"].count_documents({"workspace_id": ws, "category": "workspace_alert"}) == 1
