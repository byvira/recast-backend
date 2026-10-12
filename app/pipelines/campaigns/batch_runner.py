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
from app.models.text import ExtrasConfig, Platform, TextPipelineResult, ToneOverride
from app.pipelines.text.orchestrator import run_batch_pipeline
from app.pipelines.text.events import emit_run_completed
from app.shared.activity.runs import brand_label, run_label, tracked_run, update_run
from app.pipelines.text.storage import save_pipeline_result
from app.shared.language import resolve_content_language

from app.pipelines.campaigns import media as campaign_media
from app.shared import pipeline_runs

logger = logging.getLogger(__name__)

_CADENCE_DELTA = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1)}


async def skip_to_next_cadence(campaign_id: str) -> None:
    """Moves an automatic campaign's next batch to its next turn (a day or a week from now). A manual campaign is left alone."""
    campaign = await get_campaigns_collection().find_one({"id": campaign_id}, {"cadence": 1})
    delta = _CADENCE_DELTA.get(((campaign or {}).get("cadence") or {}).get("frequency", "manual"))
    # A cancelled batch starts fresh next time: the resume marker goes, the days it made stay.
    changes: dict[str, Any] = {"$unset": {"cadence.batch_done_days": ""}}
    if delta:
        changes["$set"] = {"cadence.next_run_at": datetime.now(timezone.utc) + delta}
    await get_campaigns_collection().update_one({"id": campaign_id}, changes)


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

    days = max(1, min(30, int(campaign.get("cadence", {}).get("days_per_batch") or 7)))
    # A batch that failed part-way saved its finished days; this attempt carries on from the first day that is missing.
    start_day = int(campaign.get("cadence", {}).get("batch_done_days") or 0)
    if start_day >= days:
        start_day = 0

    try:
        platforms = [Platform(p) for p in campaign["platforms"]]
        platforms_by_day = (
            [[Platform(p) for p in day] for day in campaign["platforms_by_day"]]
            if campaign.get("platforms_by_day") else None
        )
    except ValueError as e:
        raise ValueError(f"Invalid platform on campaign: {e}")

    # How the text is written: the campaign's own choices (a campaign made before these existed uses the old defaults).
    text_options = campaign.get("text_options") or {}
    tone_value = str(text_options.get("tone") or "brand")
    extras = ExtrasConfig(
        hook_variations=bool(text_options.get("hook_variations", True)),
        hook_count=int(text_options.get("hook_count") or 3),
        hashtags=bool(text_options.get("hashtags", True)),
        carousel_format=bool(text_options.get("carousel_format", False)),
        enforce_char_limits=bool(text_options.get("enforce_char_limits", True)),
    )

    new_piece_ids: list[str] = []
    cancelled = False

    day_started = [time.monotonic()]

    async def persist_day(day_index: int, result: TextPipelineResult) -> None:
        piece_ids: list[str] = []
        for attempt in (1, 2):
            try:
                _, piece_ids = await save_pipeline_result(result, campaign_id=campaign["id"])
                break
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to save campaign %s day %d (try %d): %s", campaign["id"], day_index + 1, attempt, e)
                if attempt == 2:
                    # Not saved: stop here, so the run says so and the next attempt starts again from this day. Carrying on would
                    # report a finished batch with a day missing.
                    raise RuntimeError(f"Day {day_index + 1} could not be saved. Nothing after it was made. Try again.") from e
        new_piece_ids.extend(piece_ids)
        # The day is saved whole and recorded on the campaign at once, so a failure later leaves the campaign consistent and the
        # next attempt resumes after it.
        await get_campaigns_collection().update_one(
            {"id": campaign["id"]},
            {"$push": {"piece_ids": {"$each": piece_ids}}, "$set": {"cadence.batch_done_days": day_index + 1, "updated_at": datetime.now(timezone.utc)}},
        )
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
                extras=extras,
                days=days,
                language=language,
                tone=ToneOverride(tone_value),
                on_day_complete=persist_day,
                before_day=(lambda _i: pipeline_runs.checkpoint(run_id)) if run_id else None,
                start_day=start_day,
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
    # A campaign that is not daily or weekly has no next automatic run: leaving an old time in place would make the scheduler start
    # it again every minute.
    update["cadence.next_run_at"] = (now + delta) if delta else None

    # The days were added to the campaign as they were saved. A finished batch clears the resume marker; a cancelled one keeps the
    # days it made and also starts fresh next time.
    await get_campaigns_collection().update_one(
        {"id": campaign["id"]},
        {"$set": update, "$unset": {"cadence.batch_done_days": ""}},
    )

    if cancelled:
        raise pipeline_runs.RunCancelled()
    return {"new_piece_ids": new_piece_ids}
