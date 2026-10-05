"""Daily check that older published posts are still on their platforms.

Posts from the last 90 days are checked every 6 hours with their numbers (see pipelines/analytics/scheduler.py). Older posts
are no longer refreshed, so a post deleted long after publishing would stay "published" with a dead link. This job asks about
them again, a few at a time: a post not asked about for 7 days, 100 per run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import content_pieces
from app.pipelines.analytics import link_health

logger = logging.getLogger(__name__)

RECHECK_AFTER = timedelta(days=7)
RECENT_WINDOW = timedelta(days=90)
PER_RUN = 100


@distributed_job_lock("link_checks", ttl_seconds=900)
async def check_old_post_links() -> int:
    """Called once a day. Returns how many posts were asked about."""
    now = datetime.now(timezone.utc)
    flt = {
        "publish_status": "published",
        "platform_post_id": {"$exists": True, "$ne": None},
        "platform_state": {"$ne": "removed"},
        "published_at": {"$lt": now - RECENT_WINDOW},
        "$or": [{"platform_checked_at": {"$exists": False}}, {"platform_checked_at": {"$lt": now - RECHECK_AFTER}}],
    }
    asked = 0
    async for piece in content_pieces.find(flt, {"_id": 0}).sort("platform_checked_at", 1).limit(PER_RUN):
        await link_health.check_now(piece["workspace_id"], piece)
        asked += 1
    return asked
