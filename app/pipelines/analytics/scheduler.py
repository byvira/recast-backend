"""
Analytics scheduler job.
Runs every 6 hours via APScheduler — fetches and stores metrics
for all published posts and connected accounts, per workspace.
"""

import logging
from datetime import datetime, timezone, timedelta

from app.core.scheduler_lock import distributed_job_lock
from app.pipelines.analytics.aggregator import (
    fetch_post_metrics_all,
    fetch_account_metrics_all,
)
from app.pipelines.analytics.snapshots import record_daily_snapshot
from app.pipelines.publish.spine import platform_key
from app.db.mongo import get_db

logger = logging.getLogger(__name__)


@distributed_job_lock("refresh_analytics", ttl_seconds=1800)
async def refresh_analytics():
    """
    Scheduled job — fetch latest metrics for every workspace with a connection.
    Runs every 6 hours.
    """
    logger.info("Analytics refresh started — %s", datetime.now(timezone.utc).isoformat())

    db = get_db()

    workspace_ids = await db["workspace_connections"].distinct("workspace_id")
    logger.info("Refreshing analytics for %d workspaces", len(workspace_ids))

    for workspace_id in workspace_ids:
        try:
            await _refresh_workspace_analytics(db, workspace_id)
        except Exception as exc:
            logger.error("Analytics refresh failed for workspace %s: %s", workspace_id, exc)

    logger.info("Analytics refresh complete")


async def _refresh_workspace_analytics(db, workspace_id: str):
    """Refresh both account and post metrics for a single workspace."""

    # ── Account metrics ───────────────────────────────────────────────────
    since = datetime.now(timezone.utc) - timedelta(days=7)
    until = datetime.now(timezone.utc)

    account_metrics = await fetch_account_metrics_all(
        workspace_id=workspace_id,
        since=since,
        until=until,
    )

    for m in account_metrics:
        await db["account_metrics"].update_one(
            {"workspace_id": workspace_id, "platform": m.platform},
            {"$set": {
                **m.model_dump(),
                "workspace_id": workspace_id,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    logger.info(
        "Account metrics saved for workspace %s — %d platforms",
        workspace_id, len(account_metrics),
    )

    # ── Post metrics ──────────────────────────────────────────────────────
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    stale_after = datetime.now(timezone.utc) - timedelta(days=90)

    # Each content_pieces document is already exactly one platform's content
    # (piece["platform"] + piece["platform_post_id"]) — see the identical
    # fix and comment in app/agents/analytics/nodes.py::fetch_metrics_node.
    # This used to read "platform_results", a field nothing ever wrote,
    # so posts_to_fetch was always empty and this job never actually
    # refreshed post metrics.
    published_posts = await db["content_pieces"].find(
        {
            "workspace_id":     workspace_id,
            "publish_status":   "published",
            "platform_post_id": {"$exists": True, "$ne": None},
            # A post that is gone from its platform is no longer checked.
            "platform_state": {"$ne": "removed"},
            "$and": [
                {"$or": [
                    {"metrics_fetched_at": {"$lt": cutoff}},
                    {"metrics_fetched_at": {"$exists": False}},
                ]},
                # Posts older than 90 days are no longer polled every run (their numbers have settled).
                {"$or": [
                    {"published_at": {"$gte": stale_after}},
                    {"published_at": {"$exists": False}},
                    {"published_at": None},
                ]},
            ],
        },
        {
            "platform": 1,
            "platform_post_id": 1,
            "piece_id": 1,
            "_id": 1,
        }
    ).sort("metrics_fetched_at", 1).to_list(length=200)  # never-fetched first, then least recently fetched

    posts_to_fetch = [
        {
            "piece_id":         piece.get("piece_id") or str(piece["_id"]),
            "platform":         platform_key(piece.get("platform", "")),
            "platform_post_id": piece["platform_post_id"],
            "platform_user_id": "",
        }
        for piece in published_posts
    ]

    if not posts_to_fetch:
        logger.info("No posts to refresh for workspace %s", workspace_id)
        await record_daily_snapshot(workspace_id)
        return

    post_metrics = await fetch_post_metrics_all(
        workspace_id=workspace_id,
        posts=posts_to_fetch,
    )

    for m in post_metrics:
        await db["post_metrics"].update_one(
            {
                "workspace_id":     workspace_id,
                "platform":         m.platform,
                "platform_post_id": m.platform_post_id,
            },
            {"$set": {
                **m.model_dump(),
                "workspace_id": workspace_id,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

        if m.post_id:
            await db["content_pieces"].update_one(
                {"workspace_id": workspace_id, "piece_id": m.post_id},
                {"$set": {"metrics_fetched_at": datetime.now(timezone.utc)}},
            )

    logger.info(
        "Post metrics saved for workspace %s — %d posts",
        workspace_id, len(post_metrics),
    )

    # Record today's totals snapshot — the real baseline
    # get_previous_totals() diffs against for the Home page's
    # week-over-week deltas. Cheap (same read /analytics/summary already
    # does) and idempotent per day.
    await record_daily_snapshot(workspace_id)
