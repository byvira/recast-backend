"""Recurring campaign auto-generation — Phase 3 of the "bulk campaigns"
architecture.

Mirrors app.workers.scheduled_posts.process_scheduled_posts exactly: an
APScheduler job (registered in app/main.py's lifespan) polls Mongo every
minute for campaigns that are due, and runs them one at a time so one
campaign's failure never blocks the rest.

A campaign is due when it opted into automation (cadence.frequency is not
"manual") and its cadence.next_run_at has arrived, and it isn't paused or
completed. generate_campaign_batch() (shared with the manual
generate-next-batch route) advances next_run_at itself after a successful
run, so this job never needs to track "what did I already do" state of
its own.
"""

import logging
from datetime import datetime, timezone

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_campaigns_collection
from app.pipelines.campaigns.batch_runner import generate_campaign_batch

logger = logging.getLogger(__name__)


@distributed_job_lock("run_due_campaign_batches", ttl_seconds=55)
async def run_due_campaign_batches() -> None:
    """Find all campaigns due for automatic batch generation and run them.
    Called every minute by the scheduler."""
    now = datetime.now(timezone.utc)

    due = await get_campaigns_collection().find({
        "deleted": {"$ne": True},
        "status": {"$nin": ["paused", "completed"]},
        "cadence.frequency": {"$ne": "manual"},
        "cadence.next_run_at": {"$lte": now},
    }).to_list(length=50)

    if not due:
        return

    logger.info("Found %d campaigns due for automatic batch generation", len(due))

    for campaign in due:
        try:
            await generate_campaign_batch(
                campaign,
                workspace_id=campaign["workspace_id"],
                # No "current user" in a cron job — attribute the run to
                # whoever created the campaign.
                user_id=campaign["created_by"],
            )
        except Exception as e:
            logger.error("Scheduled batch failed for campaign %s: %s", campaign.get("id"), e)
