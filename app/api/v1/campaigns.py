"""Campaign endpoints — Phase 1 of the "bulk campaigns" architecture.

A campaign groups multiple generation runs (today: one topic cluster ->
N days of content via the existing run_batch_pipeline()) under one
tracked entity, with real aggregate progress computed from the pieces it
generated — replacing the Pipeline page's fully-mocked view.

Phase 1 is text-only, single-platform-set per campaign — run_batch_
pipeline() itself doesn't support multiple content types or multiple
platform groups in one run yet; generalising it is Phase 2, not this
file. Workspace-scoped. Reads require membership; create/update/delete/
generate-next-batch require ``create_content`` (the same gate content
generation itself already uses — a campaign is a coordination layer on
top of pipelines that already exist, not a new generation engine).
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import brand_profiles, content_pieces, get_campaigns_collection
from app.models.campaign import (
    UNSUPPORTED_CAMPAIGN_SOURCES,
    Campaign,
    CampaignSourceType,
    CampaignStatus,
    CreateCampaignRequest,
    UpdateCampaignRequest,
)
from app.models.text import ExtrasConfig, Platform
from app.pipelines.text.orchestrator import run_batch_pipeline
from app.pipelines.text.scraper import scrape_url
from app.pipelines.text.storage import KANBAN_STAGES, compute_kanban_stage, save_pipeline_result
from app.shared.language import detect_language, first_present_or_none, user_language, workspace_language

router = APIRouter()
logger = logging.getLogger(__name__)


def _doc_to_campaign(doc: dict) -> Campaign:
    return Campaign(**{k: v for k, v in doc.items() if k not in ("_id", "deleted")})


async def _campaign_progress(workspace_id: str, campaign_id: str) -> dict[str, int]:
    """Real per-stage piece counts for a campaign, computed on read from
    content_pieces — same "don't store a driftable derived field" approach
    compute_kanban_stage() itself already uses."""
    counts: dict[str, int] = {stage: 0 for stage in KANBAN_STAGES}
    docs = await content_pieces.find(
        {"workspace_id": workspace_id, "campaign_id": campaign_id, "deleted": {"$ne": True}},
        {"archived": 1, "publish_status": 1, "approval_status": 1},
    ).to_list(length=None)
    for doc in docs:
        stage = compute_kanban_stage(doc)
        counts[stage] = counts.get(stage, 0) + 1
    counts["total"] = len(docs)
    return counts


@router.post("/", response_model=Campaign, status_code=201)
@limiter.limit("20/minute")
async def create_campaign(
    request: Request,
    body: CreateCampaignRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> Campaign:
    brand = await brand_profiles.find_one({"id": body.brand_id, "workspace_id": ctx.workspace_id})
    if not brand:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    if body.source_type in UNSUPPORTED_CAMPAIGN_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{body.source_type.value.replace('_', ' ').title()} campaigns aren't "
                "available yet — audio/video transcription isn't wired up. Use a topic "
                "brief or article URL for now."
            ),
        )

    try:
        platforms = [Platform(p) for p in body.platforms]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid platform: {e}")
    if not platforms:
        raise HTTPException(status_code=400, detail="At least one platform is required.")

    topic_cluster = body.topic_cluster
    source_url = None
    if body.source_type == CampaignSourceType.ARTICLE_URL:
        source_url = body.topic_cluster
        try:
            topic_cluster = await scrape_url(source_url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid4()),
        "workspace_id": ctx.workspace_id,
        "brand_id": body.brand_id,
        "name": body.name,
        "topic_cluster": topic_cluster,
        "source_type": body.source_type.value,
        "source_url": source_url,
        "content_types": ["text"],  # Phase 1 — see module docstring
        "platforms": [p.value for p in platforms],
        "cadence": body.cadence.model_dump(),
        "status": CampaignStatus.DRAFT.value,
        "piece_ids": [],
        "created_by": ctx.user_id,
        "created_at": now,
        "updated_at": now,
        "deleted": False,
    }
    await get_campaigns_collection().insert_one(doc)
    return _doc_to_campaign(doc)


@router.get("/")
@limiter.limit("60/minute")
async def list_campaigns(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> list[dict[str, Any]]:
    """
    Includes each campaign's real progress inline (same computation
    GET /{campaign_id} does) — the Pipeline page's job list needs each
    job's real status/branch data to filter/render without an N+1 query
    per card.
    """
    docs = await get_campaigns_collection().find(
        {"workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
        sort=[("updated_at", -1)],
    ).to_list(length=200)
    out = []
    for doc in docs:
        progress = await _campaign_progress(ctx.workspace_id, doc["id"])
        out.append({**_doc_to_campaign(doc).model_dump(), "progress": progress})
    return out


@router.get("/{campaign_id}")
@limiter.limit("60/minute")
async def get_campaign(
    request: Request,
    campaign_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    doc = await get_campaigns_collection().find_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    progress = await _campaign_progress(ctx.workspace_id, campaign_id)
    return {**_doc_to_campaign(doc).model_dump(), "progress": progress}


@router.patch("/{campaign_id}", response_model=Campaign)
@limiter.limit("30/minute")
async def update_campaign(
    request: Request,
    campaign_id: str,
    body: UpdateCampaignRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> Campaign:
    existing = await get_campaigns_collection().find_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    update: dict[str, Any] = {}
    payload = body.model_dump(exclude_unset=True)
    if "platforms" in payload and payload["platforms"] is not None:
        try:
            update["platforms"] = [Platform(p).value for p in payload["platforms"]]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid platform: {e}")
    for field in ("name", "topic_cluster", "status", "cadence"):
        if field in payload and payload[field] is not None:
            update[field] = payload[field]

    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    update["updated_at"] = datetime.now(timezone.utc)
    await get_campaigns_collection().update_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id}, {"$set": update}
    )
    updated = await get_campaigns_collection().find_one({"id": campaign_id, "workspace_id": ctx.workspace_id})
    return _doc_to_campaign(updated)


@router.delete("/{campaign_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_campaign(
    request: Request,
    campaign_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> None:
    result = await get_campaigns_collection().update_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
        {"$set": {"deleted": True, "updated_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Campaign not found.")


@router.post("/{campaign_id}/generate-next-batch")
@limiter.limit("5/minute")
async def generate_next_batch(
    request: Request,
    campaign_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """
    Run one more batch for this campaign — the existing run_batch_pipeline()
    (topic cluster -> N days of content, one platform set, already used by
    /text/batch), tagging every resulting piece with this campaign_id.
    Expensive; rate limited same as /text/batch.
    """
    campaign = await get_campaigns_collection().find_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    if campaign.get("content_types") != ["text"]:
        raise HTTPException(
            status_code=400,
            detail="Only text campaigns are supported right now.",
        )

    brand = await brand_profiles.find_one({"id": campaign["brand_id"], "workspace_id": ctx.workspace_id})
    if not brand or not brand.get("is_complete"):
        raise HTTPException(
            status_code=400,
            detail="Brand profile is not complete. Finish onboarding first.",
        )

    language = first_present_or_none(
        await workspace_language(ctx.workspace_id),
        await user_language(ctx.user_id),
    ) or detect_language(campaign["topic_cluster"]) or "en"

    days = campaign.get("cadence", {}).get("days_per_batch", 7)
    platforms = [Platform(p) for p in campaign["platforms"]]

    try:
        results = await run_batch_pipeline(
            topic_cluster=campaign["topic_cluster"],
            platforms=platforms,
            brand_id=campaign["brand_id"],
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            extras=ExtrasConfig(),
            days=days,
            language=language,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Campaign batch generation error: {str(e)}")

    new_piece_ids: list[str] = []
    for i, day_result in enumerate(results):
        try:
            _, piece_ids = await save_pipeline_result(
                day_result,
                campaign_id=campaign_id,
            )
            new_piece_ids.extend(piece_ids)
        except Exception as e:
            logger.error(
                "Failed to save campaign %s day %d: %s", campaign_id, i + 1, e,
            )

    now = datetime.now(timezone.utc)
    await get_campaigns_collection().update_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id},
        {
            "$push": {"piece_ids": {"$each": new_piece_ids}},
            "$set": {
                "updated_at": now,
                **({"status": CampaignStatus.ACTIVE.value} if campaign["status"] == CampaignStatus.DRAFT.value else {}),
            },
        },
    )

    updated = await get_campaigns_collection().find_one({"id": campaign_id, "workspace_id": ctx.workspace_id})
    progress = await _campaign_progress(ctx.workspace_id, campaign_id)
    return {
        **_doc_to_campaign(updated).model_dump(),
        "progress": progress,
        "pieces_generated_this_run": len(new_piece_ids),
    }
