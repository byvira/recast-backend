"""Keeping video storage and records tidy, daily.

Only things nobody can use any more are removed: files left by uploads that were never finished, preview files whose library item
is gone, and finished job records past their useful life. A member's own uploads and finished videos are never touched here;
removing those is the member's choice, or a workspace purge.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_db, media_assets
from app.shared.storage import delete_file_by_url

logger = logging.getLogger(__name__)

#: Uploads that were started and never finished are removed once they are this old.
STALE_UPLOAD_DAYS = 2
#: Finished render and preview job records are kept this long (the finished video itself is a library item and stays).
JOB_RECORD_DAYS = 90
#: Decision-log entries are kept this long.
DECISION_DAYS = 180
#: Webhook delivery records are kept this long.
WEBHOOK_DELIVERY_DAYS = 30
#: A review link that has expired or been ended is kept this long (with its comments and answers), so the member can still read what the client said.
REVIEW_LINK_DAYS = 90
PER_RUN = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def sweep_unfinished_uploads(now: datetime | None = None) -> int:
    """Close uploads that never completed and remove the half-sent files they left in storage."""
    db = get_db()
    cutoff = (now or _now()) - timedelta(days=STALE_UPLOAD_DAYS)
    removed = 0
    async for up in db["video_uploads"].find({"status": {"$in": ["started", "expired", "cancelled", "failed"]}, "created_at": {"$lt": cutoff}}).limit(PER_RUN):
        public_id = up.get("public_id")
        if public_id:
            try:
                import asyncio

                import cloudinary.uploader

                from app.shared.storage import _ensure_configured

                _ensure_configured()
                await asyncio.to_thread(lambda pid=public_id: cloudinary.uploader.destroy(pid, resource_type="video"))
            except Exception as exc:  # noqa: BLE001 - tried again tomorrow
                logger.warning("Could not remove the leftover of upload %s: %s", up.get("id"), exc)
                continue
        await db["video_uploads"].delete_one({"id": up["id"]})
        removed += 1
    return removed


async def sweep_orphaned_previews() -> int:
    """Preview files whose library item no longer exists. A preview job that finished after its item was deleted leaves these."""
    db = get_db()
    removed = 0
    async for job in db["video_jobs"].find({"kind": "proxy", "status": "done"}, {"source_media_id": 1, "workspace_id": 1, "report": 1, "id": 1}).limit(PER_RUN):
        media_id = job.get("source_media_id")
        if media_id and await media_assets.find_one({"id": media_id}, {"id": 1}):
            continue
        # the library item is gone: its preview material goes with it, and the job record is no longer needed
        for out in (job.get("outputs") or []):
            if isinstance(out, dict) and out.get("url"):
                await delete_file_by_url(out["url"])
        await db["video_jobs"].delete_one({"id": job["id"]})
        removed += 1
    return removed


async def trim_records(now: datetime | None = None) -> dict[str, int]:
    db = get_db()
    moment = now or _now()
    jobs = await db["video_jobs"].delete_many({
        "status": {"$in": ["done", "failed", "cancelled", "blocked"]}, "finished_at": {"$lt": moment - timedelta(days=JOB_RECORD_DAYS)},
    })
    decisions = await db["agent_decisions"].delete_many({"created_at": {"$lt": moment - timedelta(days=DECISION_DAYS)}, "outcome": {"$ne": "needs_decision"}})
    deliveries = await db["video_webhook_deliveries"].delete_many({
        "status": {"$in": ["delivered", "failed"]}, "created_at": {"$lt": moment - timedelta(days=WEBHOOK_DELIVERY_DAYS)}})
    old_links = [lk["id"] async for lk in db["video_review_links"].find(
        {"$or": [{"revoked": True, "revoked_at": {"$lt": moment - timedelta(days=REVIEW_LINK_DAYS)}}, {"expires_at": {"$lt": moment - timedelta(days=REVIEW_LINK_DAYS)}}]}, {"id": 1}).limit(PER_RUN)]
    if old_links:
        for name in ("video_review_comments", "video_review_decisions"):
            await db[name].delete_many({"link_id": {"$in": old_links}})
        await db["video_review_links"].delete_many({"id": {"$in": old_links}})
    return {"job_records": jobs.deleted_count, "decisions": decisions.deleted_count, "webhook_deliveries": deliveries.deleted_count, "review_links": len(old_links)}


@distributed_job_lock("video_transcripts", ttl_seconds=900)
async def continue_video_transcripts() -> int:
    """Every few minutes: carry on transcripts that are waiting for the free speech allowance."""
    from app.video import transcribe

    return await transcribe.continue_waiting()


@distributed_job_lock("video_retention", ttl_seconds=1800)
async def run_video_retention() -> dict[str, int]:
    """Daily: unfinished uploads, orphaned previews, and old records."""
    from app.video import deletion

    out = {"uploads": 0, "previews": 0, "files_retried": 0}
    for name, fn in (("uploads", sweep_unfinished_uploads), ("previews", sweep_orphaned_previews), ("files_retried", deletion.retry_failed_files)):
        try:
            out[name] = await fn()
        except Exception as exc:  # noqa: BLE001 - one part failing must not stop the others; it is tried again tomorrow
            logger.error("Video retention (%s) failed: %s", name, exc)
    try:
        out.update(await trim_records())
    except Exception as exc:  # noqa: BLE001
        logger.error("Video retention (records) failed: %s", exc)
    if any(out.values()):
        logger.info("Video retention removed: %s", out)
    return out
