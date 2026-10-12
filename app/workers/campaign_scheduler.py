"""Recurring campaign auto-generation — Phase 3 of the "bulk campaigns"
architecture.

Mirrors app.workers.scheduled_posts.process_scheduled_posts exactly: an
APScheduler job (registered in app/main.py's lifespan) polls Mongo every
minute for campaigns that are due, and runs them one at a time so one
campaign's failure never blocks the rest.

A campaign is due when it opted into automation (cadence.frequency is not
"manual") and its cadence.next_run_at has arrived, and it isn't paused or
completed. Each due campaign is started as a saved background run (the same
as a manual one) and the tick returns at once; generate_campaign_batch()
advances next_run_at itself after a successful run, and an active run for the
campaign stops it being started twice.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.agents.supervisor.service import assert_generation_allowed
from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_campaigns_collection
from app.pipelines.campaigns.batch_runner import generate_campaign_batch
from app.shared.activity import record_system

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
        # An owner-paused workspace isn't a failing campaign: skip without
        # touching next_run_at, so it runs on the next tick after disarming
        # instead of being backed off (and logged as "couldn't generate").
        try:
            await assert_generation_allowed(campaign["workspace_id"])
        except HTTPException:
            logger.info(
                "Skipping scheduled campaign %s — workspace %s has generation paused.",
                campaign.get("id"), campaign["workspace_id"],
            )
            continue

        # A scheduled batch is a saved run like a manual one: it shows in Control Tower, can be paused or cancelled, ends with a note
        # in Activity, and while it is going the campaign is not started a second time (it stays "due" until the batch finishes).
        try:
            await _start_batch_run(campaign)
        except HTTPException as e:
            # The workspace already has its limit of jobs going: try again on a later tick, without counting it as a failure.
            logger.info("Scheduled campaign %s waits: %s", campaign.get("id"), e.detail)
        except Exception as e:  # noqa: BLE001
            logger.error("Scheduled batch could not start for campaign %s: %s", campaign.get("id"), e)
            await _back_off(campaign, str(e))


async def _start_batch_run(campaign: dict) -> None:
    from app.shared import pipeline_runs

    workspace_id = campaign["workspace_id"]
    if await pipeline_runs.active_run_for(workspace_id, "campaign", {"campaign_id": campaign["id"]}):
        return  # its last batch is still going
    from app.db.mongo import workspace_members

    creator = await workspace_members.find_one({"workspace_id": workspace_id, "user_id": campaign["created_by"]})
    if not creator or creator.get("status", "active") != "active":
        # The campaign runs as the person who made it. If they have left (or been removed or suspended), it is paused instead of
        # carrying on with their rights; an owner can hand it to someone else by editing it.
        reason = "The person who made this campaign is no longer in the workspace, so it was paused."
        await get_campaigns_collection().update_one(
            {"id": campaign["id"]},
            {"$set": {"status": "paused", "cadence.next_run_at": None, "cadence.last_error": reason, "updated_at": datetime.now(timezone.utc)}},
        )
        await record_system(
            workspace_id=workspace_id, key=f"campaign_run:{campaign['id']}", actor_name="Campaign scheduler",
            category="content_generated", title=f"Campaign \"{campaign.get('name') or 'Untitled'}\" was paused", description=reason,
            status="warning", target_id=campaign["id"], target_type="Campaign", target_label=campaign.get("name") or None,
            href="/dashboard/campaigns",
        )
        return
    await pipeline_runs.assert_capacity(workspace_id)
    days = (campaign.get("cadence") or {}).get("days_per_batch", 7)
    try:
        doc = await pipeline_runs.create_run(
            workspace_id=workspace_id, user_id=campaign["created_by"], kind="campaign",
            title=f"Campaign \"{campaign.get('name') or 'Untitled'}\"", ref={"campaign_id": campaign["id"]},
            steps_total=days, href=f"/dashboard/pipelines/new?campaignId={campaign['id']}",
            active_key=f"campaign:{campaign['id']}",
        )
    except pipeline_runs.RunAlreadyActive:
        return  # a batch for this campaign is already going

    async def work(run: dict) -> dict:
        try:
            # No "current user" in a cron job: the run is attributed to whoever created the campaign.
            result = await generate_campaign_batch(
                campaign, workspace_id=workspace_id, user_id=campaign["created_by"], run_id=run["id"],
            )
        except pipeline_runs.RunCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            # A deliberate rejection (e.g. the AI budget's 403) carries a readable detail; str() of an HTTPException is "403: ...".
            reason = e.detail if isinstance(e, HTTPException) else str(e)
            logger.error("Scheduled batch failed for campaign %s: %s", campaign.get("id"), reason)
            await _back_off(campaign, str(reason))
            raise
        return {"new_piece_count": len(result["new_piece_ids"])}

    pipeline_runs.start(doc, work)


#: A failed run used to leave next_run_at in the past, so this job re-ran the
#: same failing (LLM-costing) campaign every minute indefinitely. Back off
#: exponentially instead: 1h, 2h, 4h … capped at a day.
_BACKOFF_CAP = timedelta(hours=24)


async def _back_off(campaign: dict, error: str) -> None:
    failures = int((campaign.get("cadence") or {}).get("failures") or 0) + 1
    delay = min(timedelta(hours=2 ** (failures - 1)), _BACKOFF_CAP)
    retry_at = datetime.now(timezone.utc) + delay
    await get_campaigns_collection().update_one(
        {"id": campaign["id"]},
        {"$set": {"cadence.failures": failures, "cadence.next_run_at": retry_at,
                  "cadence.last_error": error[:500]}},
    )
    hours = round(delay.total_seconds() / 3600)
    await record_system(
        workspace_id=campaign["workspace_id"],
        key=f"campaign_run:{campaign['id']}",
        actor_name="Campaign scheduler",
        category="content_generated",
        title=f"Campaign \"{campaign.get('name') or 'Untitled'}\" couldn't generate",
        description=f"{error} Ekavo will try again in {hours} hour{'s' if hours != 1 else ''}.",
        status="failed",
        target_id=campaign["id"],
        target_type="Campaign",
        target_label=campaign.get("name") or None,
        href="/dashboard/campaigns",
        metadata={"retryAttempt": failures},
    )
