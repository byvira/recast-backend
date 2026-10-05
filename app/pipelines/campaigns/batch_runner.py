"""Shared campaign-batch business logic.

Extracted from app.api.v1.campaigns.generate_next_batch so the same logic
can be called both from that route (a user clicking "Generate Next Batch")
and from app.workers.campaign_scheduler (an APScheduler job regenerating
campaigns automatically on their cadence). Raises ValueError on business-
rule failures rather than HTTPException — the route translates that to a
400, the scheduler just logs and moves on to the next due campaign.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.mongo import brand_profiles, get_campaigns_collection
from app.models.campaign import CampaignStatus
from app.models.text import ExtrasConfig, Platform, TextPipelineResult
from app.pipelines.text.orchestrator import run_batch_pipeline
from app.pipelines.text.events import emit_run_completed
from app.shared.activity.runs import brand_label, run_label, tracked_run, update_run
from app.pipelines.text.storage import save_pipeline_result
from app.shared.language import resolve_content_language

from app.pipelines.campaigns import media as campaign_media
from app.shared import pipeline_runs

logger = logging.getLogger(__name__)

_CADENCE_DELTA = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1)}


async def generate_campaign_batch(
    campaign: dict[str, Any], *, workspace_id: str, user_id: str, run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run one more batch for `campaign`, tagging every resulting piece with
    its campaign_id. Returns {"new_piece_ids": [...]}. Raises ValueError for
    every business-rule failure (unsupported content type, incomplete
    brand, invalid stored platform) — never an HTTPException, since the
    scheduler has no request to attach one to.

    With `run_id` (a saved background run), the batch waits while the run is paused and stops when it is cancelled, between
    days. Days already made are kept either way, and RunCancelled is raised once they are saved."""
    if "text" not in (campaign.get("content_types") or ["text"]):
        raise ValueError("A campaign always makes text; media is added on top of it.")

    brand = await brand_profiles.find_one({"id": campaign["brand_id"], "workspace_id": workspace_id})
    if not brand or not brand.get("is_complete"):
        raise ValueError("Brand profile is not complete. Finish onboarding first.")

    language = await resolve_content_language(
        campaign=campaign.get("language"), brand=brand.get("language"),
        workspace_id=workspace_id, user_id=user_id, text=campaign["topic_cluster"],
    )

    days = campaign.get("cadence", {}).get("days_per_batch", 7)

    try:
        platforms = [Platform(p) for p in campaign["platforms"]]
        platforms_by_day = (
            [[Platform(p) for p in day] for day in campaign["platforms_by_day"]]
            if campaign.get("platforms_by_day") else None
        )
    except ValueError as e:
        raise ValueError(f"Invalid platform on campaign: {e}")

    new_piece_ids: list[str] = []
    cancelled = False

    day_started = [time.monotonic()]

    async def persist_day(day_index: int, result: TextPipelineResult) -> None:
        try:
            _, piece_ids = await save_pipeline_result(result, campaign_id=campaign["id"])
            new_piece_ids.extend(piece_ids)
        except Exception as e:
            logger.error("Failed to save campaign %s day %d: %s", campaign["id"], day_index + 1, e)
            piece_ids = []
        # Media is an extra on top of the saved text: it only runs when the campaign asked for it,
        # and a failure there is logged and never undoes the day's text.
        if piece_ids and campaign_media.wanted_kinds(campaign.get("media_plan")):
            try:
                await campaign_media.generate_media_for_pieces(
                    campaign, piece_ids, workspace_id=workspace_id, user_id=user_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("Campaign %s day %d media failed: %s", campaign["id"], day_index + 1, e)
        # Autonomous run — attributed to the campaign scheduler in the
        # Activity Log, with the member who owns the campaign as the subject.
        await emit_run_completed(
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=result.session_id,
            platforms=[
                p.platform.value if hasattr(p.platform, "value") else str(p.platform)
                for p in result.pieces if (p.content or "").strip()
            ],
            requested=len(platforms_by_day[day_index]) if platforms_by_day else len(platforms),
            duration_ms=int((time.monotonic() - day_started[0]) * 1000),
            brand_id=campaign["brand_id"],
            # PAR-014: campaigns run headless (no emitter, no live SSE
            # viewer) — this title, surfaced in the Activity Log, is the
            # only place a degraded day (repeated topic due to a planning
            # failure) reaches the user at all.
            title=(
                f"{campaign.get('name') or campaign['topic_cluster']} — day {day_index + 1}"
                + (" ⚠ repeated topic — day-angle planning failed" if result.angle_planning_degraded else "")
            ),
            trigger="campaign",
        )
        day_started[0] = time.monotonic()
        await update_run(workspace_id, f"campaign:{campaign['id']}", steps_done=day_index + 1)
        if run_id:
            await pipeline_runs.mark_progress(run_id, day_index + 1, days)

    async with tracked_run(
        workspace_id=workspace_id, run_id=f"campaign:{campaign['id']}", kind="campaign",
        title=campaign.get("name") or run_label(campaign["topic_cluster"]),
        project=brand_label(brand), steps_total=days,
    ):
        try:
            await run_batch_pipeline(
                topic_cluster=campaign["topic_cluster"],
                platforms=platforms,
                platforms_by_day=platforms_by_day,
                brand_id=campaign["brand_id"],
                workspace_id=workspace_id,
                user_id=user_id,
                extras=ExtrasConfig(),
                days=days,
                language=language,
                on_day_complete=persist_day,
                before_day=(lambda _i: pipeline_runs.checkpoint(run_id)) if run_id else None,
            )
        except pipeline_runs.RunCancelled:
            cancelled = True

    now = datetime.now(timezone.utc)
    frequency = campaign.get("cadence", {}).get("frequency", "manual")
    update: dict[str, Any] = {
        "updated_at": now,
        "last_generated_at": now,
        "cadence.failures": 0,
        **({"status": CampaignStatus.ACTIVE.value} if campaign["status"] == CampaignStatus.DRAFT.value else {}),
    }
    delta = _CADENCE_DELTA.get(frequency)
    if delta:
        update["cadence.next_run_at"] = now + delta

    await get_campaigns_collection().update_one(
        {"id": campaign["id"]},
        {"$push": {"piece_ids": {"$each": new_piece_ids}}, "$set": update},
    )

    if cancelled:
        raise pipeline_runs.RunCancelled()
    return {"new_piece_ids": new_piece_ids}
