"""
Analytics scheduler job.
Runs every 6 hours via APScheduler — fetches and stores metrics
for all published posts and connected accounts, per workspace.
"""

import logging
from datetime import datetime, timezone, timedelta

from app.pipelines.analytics.aggregator import (
    fetch_post_metrics_all,
    fetch_account_metrics_all,
)
from app.db.mongo import get_db

logger = logging.getLogger(__name__)


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

    published_posts = await db["content_pieces"].find(
        {
            "workspace_id":   workspace_id,
            "publish_status": "published",
            "$or": [
                {"metrics_fetched_at": {"$lt": cutoff}},
                {"metrics_fetched_at": {"$exists": False}},
            ],
        },
        {
            "platform_results": 1,
            "_id": 1,
        }
    ).to_list(length=200)

    posts_to_fetch = []
    for piece in published_posts:
        for result in piece.get("platform_results", []):
            if result.get("platform_post_id"):
                posts_to_fetch.append({
                    "piece_id":          str(piece["_id"]),
                    "platform":          result["platform"],
                    "platform_post_id":  result["platform_post_id"],
                    "platform_user_id":  result.get("platform_user_id", ""),
                })

    if not posts_to_fetch:
        logger.info("No posts to refresh for workspace %s", workspace_id)
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
                {"_id": m.post_id},
                {"$set": {"metrics_fetched_at": datetime.now(timezone.utc)}},
            )

    logger.info(
        "Post metrics saved for workspace %s — %d posts",
        workspace_id, len(post_metrics),
    )
