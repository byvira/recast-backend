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
from typing import Any

from app.db.mongo import brand_profiles, get_campaigns_collection
from app.models.campaign import CampaignStatus
from app.models.text import ExtrasConfig, Platform, TextPipelineResult
from app.pipelines.text.orchestrator import run_batch_pipeline
from app.pipelines.text.events import emit_run_completed
from app.shared.activity.runs import brand_label, run_label, tracked_run, update_run
from app.pipelines.text.storage import save_pipeline_result
from app.shared.language import detect_language, first_present_or_none, user_language, workspace_language

logger = logging.getLogger(__name__)

_CADENCE_DELTA = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1)}


async def generate_campaign_batch(
    campaign: dict[str, Any], *, workspace_id: str, user_id: str,
) -> dict[str, Any]:
    """Run one more batch for `campaign`, tagging every resulting piece with
    its campaign_id. Returns {"new_piece_ids": [...]}. Raises ValueError for
    every business-rule failure (unsupported content type, incomplete
    brand, invalid stored platform) — never an HTTPException, since the
    scheduler has no request to attach one to."""
    if campaign.get("content_types") != ["text"]:
        raise ValueError("Only text campaigns are supported right now.")

    brand = await brand_profiles.find_one({"id": campaign["brand_id"], "workspace_id": workspace_id})
    if not brand or not brand.get("is_complete"):
        raise ValueError("Brand profile is not complete. Finish onboarding first.")

    language = first_present_or_none(
        await workspace_language(workspace_id),
        await user_language(user_id),
    ) or detect_language(campaign["topic_cluster"]) or "en"

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

    day_started = [time.monotonic()]

    async def persist_day(day_index: int, result: TextPipelineResult) -> None:
        try:
            _, piece_ids = await save_pipeline_result(result, campaign_id=campaign["id"])
            new_piece_ids.extend(piece_ids)
        except Exception as e:
            logger.error("Failed to save campaign %s day %d: %s", campaign["id"], day_index + 1, e)
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
            title=f"{campaign.get('name') or campaign['topic_cluster']} — day {day_index + 1}",
            trigger="campaign",
        )
        day_started[0] = time.monotonic()
        await update_run(workspace_id, f"campaign:{campaign['id']}", steps_done=day_index + 1)

    async with tracked_run(
        workspace_id=workspace_id, run_id=f"campaign:{campaign['id']}", kind="campaign",
        title=campaign.get("name") or run_label(campaign["topic_cluster"]),
        project=brand_label(brand), steps_total=days,
    ):
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
        )

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

    return {"new_piece_ids": new_piece_ids}
