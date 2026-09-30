"""Campaign endpoints — the "bulk campaigns" architecture.

A campaign groups multiple generation runs (one topic cluster -> N days
of content via run_batch_pipeline(), optionally varying platforms per
day via `platforms_by_day`) under one tracked entity, with real aggregate
progress computed from the pieces it generated, and optional recurring
auto-generation (cadence.frequency + cadence.next_run_at, polled by
app.workers.campaign_scheduler).

Still text-only — content_types is hardcoded to ["text"] at creation.
Workspace-scoped. Reads require membership; create/update/delete/
generate-next-batch require ``create_content`` (the same gate content
generation itself already uses — a campaign is a coordination layer on
top of pipelines that already exist, not a new generation engine).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

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
from app.models.text import Platform
from app.pipelines.campaigns import media as campaign_media_module
from app.pipelines.campaigns.batch_runner import generate_campaign_batch
from app.pipelines.export import campaign_archive
from app.pipelines.campaigns.suggest import suggest_campaign_topics
from app.pipelines.text.brand_context import build_brand_context
from app.pipelines.text.scraper import scrape_url
from app.pipelines.text.storage import KANBAN_STAGES, compute_kanban_stage
from app.shared.storage import ContentType as UploadContentType, upload_file

MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024  # 5MB
ALLOWED_THUMBNAIL_TYPES = {"image/jpeg", "image/png", "image/webp"}

router = APIRouter()
logger = logging.getLogger(__name__)


def _content_types(media_plan: dict) -> list[str]:
    """Text always, plus each media kind that is switched on and can be generated today. Video is
    kept in the media plan (set up, inactive) but is not generated yet, so it is not listed here."""
    kinds = media_plan.get("kinds") or [] if media_plan.get("enabled") else []
    return ["text"] + [k for k in ("audio", "image") if k in kinds]


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


async def _campaign_progress_bulk(
    workspace_id: str, campaign_ids: list[str]
) -> dict[str, dict[str, int]]:
    """Same per-stage counts as _campaign_progress, for every campaign in
    one query instead of one per campaign — list_campaigns' docstring
    already claimed this, but actually called _campaign_progress in a loop
    (one content_pieces query per campaign card)."""
    def _empty_counts() -> dict[str, int]:
        counts = {stage: 0 for stage in KANBAN_STAGES}
        counts["total"] = 0
        return counts

    progress = {cid: _empty_counts() for cid in campaign_ids}
    if not campaign_ids:
        return progress

    docs = await content_pieces.find(
        {
            "workspace_id": workspace_id,
            "campaign_id": {"$in": campaign_ids},
            "deleted": {"$ne": True},
        },
        {"archived": 1, "publish_status": 1, "approval_status": 1, "campaign_id": 1},
    ).to_list(length=None)

    for doc in docs:
        cid = doc.get("campaign_id")
        if cid not in progress:
            continue
        stage = compute_kanban_stage(doc)
        progress[cid][stage] = progress[cid].get(stage, 0) + 1
        progress[cid]["total"] += 1

    return progress


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
    cadence = body.cadence.model_dump()
    # next_run_at is server-computed only — CampaignCadence is embedded
    # directly in the request body, so a client could otherwise set it
    # itself and jump the scheduler's queue.
    cadence["next_run_at"] = now if cadence.get("frequency") != "manual" else None
    doc = {
        "id": str(uuid4()),
        "workspace_id": ctx.workspace_id,
        "brand_id": body.brand_id,
        "name": body.name,
        "topic_cluster": topic_cluster,
        "source_type": body.source_type.value,
        "source_url": source_url,
        "content_types": _content_types(body.media_plan.model_dump()),
        "platforms": [p.value for p in platforms],
        "platforms_by_day": body.platforms_by_day,
        "cadence": cadence,
        "media_plan": body.media_plan.model_dump(),
        "status": CampaignStatus.DRAFT.value,
        "piece_ids": [],
        "last_generated_at": None,
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
    progress_by_id = await _campaign_progress_bulk(ctx.workspace_id, [doc["id"] for doc in docs])
    return [
        {**_doc_to_campaign(doc).model_dump(), "progress": progress_by_id[doc["id"]]}
        for doc in docs
    ]


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
    for field in ("name", "topic_cluster", "status", "cadence", "platforms_by_day"):
        if field in payload and payload[field] is not None:
            update[field] = payload[field]

    if payload.get("media_plan") is not None:
        update["media_plan"] = payload["media_plan"]
        update["content_types"] = _content_types(payload["media_plan"])

    if "cadence" in update:
        # Same server-computed-only rule as create_campaign — recompute
        # next_run_at from the new frequency rather than trust the client.
        now = datetime.now(timezone.utc)
        update["cadence"]["next_run_at"] = now if update["cadence"].get("frequency") != "manual" else None

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


async def _generated_media_by_piece(piece_ids: list[str], workspace_id: str) -> dict[str, list[dict]]:
    """Each post's generated audio and images (found by source_piece_id), keyed by piece id."""
    from app.db.mongo import audio_assets, image_assets, media_assets

    if not piece_ids:
        return {}
    query = {"workspace_id": workspace_id, "source_piece_id": {"$in": piece_ids}}
    audio = await audio_assets.find(query).to_list(length=None)
    images = await image_assets.find(query).to_list(length=None)
    media_ids = {a.get("media_id") for a in audio if a.get("media_id")}
    for doc in images:
        media_ids.update(s.get("media_id") for s in doc.get("slides", []) if s.get("media_id"))
    docs = await media_assets.find({"id": {"$in": list(media_ids)}, "workspace_id": workspace_id}).to_list(length=None)
    by_id = {m["id"]: m for m in docs}

    found: dict[str, list[dict]] = {}
    for a in audio:
        m = by_id.get(a.get("media_id") or "")
        if m and m.get("url"):
            found.setdefault(a["source_piece_id"], []).append(
                {"kind": "audio", "url": m["url"], "mime_type": m.get("mime_type"), "title": a.get("title")}
            )
    for doc in images:
        for slide in doc.get("slides", []):
            m = by_id.get(slide.get("media_id") or "")
            if m and m.get("url"):
                found.setdefault(doc["source_piece_id"], []).append(
                    {"kind": "image", "url": m["url"], "mime_type": m.get("mime_type"), "title": doc.get("title")}
                )
    return found


async def _attach_generated_media(pieces: list[dict], workspace_id: str) -> None:
    """Add each post's generated audio and images to its media list (for the export)."""
    found = await _generated_media_by_piece([p["piece_id"] for p in pieces if p.get("piece_id")], workspace_id)
    for p in pieces:
        p["media"] = list(p.get("media") or []) + [
            {"url": m["url"], "mime_type": m["mime_type"], "kind": m["kind"]} for m in found.get(p.get("piece_id"), [])
        ]


@router.get("/{campaign_id}/media")
@limiter.limit("60/minute")
async def campaign_media(
    request: Request,
    campaign_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """The audio and images made for this campaign's posts.
    {"items": {piece_id: [{kind, url, mime_type, title}]}, "status": {piece_id: {"image"|"audio": "ready"|"failed"}}}"""
    doc = await get_campaigns_collection().find_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    pieces = await content_pieces.find(
        {"workspace_id": ctx.workspace_id, "campaign_id": campaign_id, "deleted": {"$ne": True}},
        {"piece_id": 1, "media_status": 1},
    ).to_list(length=None)
    ids = [p["piece_id"] for p in pieces if p.get("piece_id")]
    return {
        "items": await _generated_media_by_piece(ids, ctx.workspace_id),
        "status": {p["piece_id"]: p["media_status"] for p in pieces if p.get("piece_id") and p.get("media_status")},
    }


@router.post("/{campaign_id}/pieces/{piece_id}/retry-media")
@limiter.limit("10/minute")
async def retry_post_media(
    request: Request,
    campaign_id: str,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """Try again the media that failed for one post. Only the failed kinds are made again."""
    doc = await get_campaigns_collection().find_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    states = await campaign_media_module.retry_media_for_piece(
        doc, piece_id, workspace_id=ctx.workspace_id, user_id=ctx.user_id,
    )
    return {"status": states}


@router.get("/{campaign_id}/export")
@limiter.limit("10/minute")
async def export_campaign(
    request: Request,
    campaign_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Response:
    """One ZIP: a .docx with every post's text (each naming where its media is) and a media/
    folder per post. Media that cannot be downloaded is marked in the document, not fatal."""
    from app.api.v1.content import _fetch_media  # same downloader the library export uses

    doc = await get_campaigns_collection().find_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    pieces = await content_pieces.find(
        {"workspace_id": ctx.workspace_id, "campaign_id": campaign_id, "deleted": {"$ne": True}},
    ).sort("created_at", 1).to_list(length=1000)

    await _attach_generated_media(pieces, ctx.workspace_id)
    posts = campaign_archive.plan_campaign_export(pieces)
    items = [m for p in posts for m in p.media][:200]
    media_bytes: dict[str, bytes] = {}
    if items:
        limit = asyncio.Semaphore(6)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            results = await asyncio.gather(*(_fetch_media(m.url, client, limit) for m in items))
        media_bytes = {m.path: data for m, data in zip(items, results) if data is not None}

    body = campaign_archive.build_zip(doc.get("name") or "Campaign", posts, media_bytes)
    return Response(
        content=body,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="recast-campaign.zip"'},
    )


@router.post("/{campaign_id}/generate-next-batch")
@limiter.limit("5/minute")
async def generate_next_batch(
    request: Request,
    campaign_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """
    Run one more batch for this campaign via generate_campaign_batch() —
    the same helper app.workers.campaign_scheduler calls automatically for
    campaigns with a non-manual cadence. Expensive; rate limited same as
    /text/batch.
    """
    campaign = await get_campaigns_collection().find_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    try:
        result = await generate_campaign_batch(campaign, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        # QA-003: was f"Campaign batch generation error: {str(e)}" returned
        # straight to the client — logged server-side instead.
        logger.error("Campaign batch generation failed for campaign %s: %s", campaign_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Batch generation failed. Please try again.")

    updated = await get_campaigns_collection().find_one({"id": campaign_id, "workspace_id": ctx.workspace_id})
    progress = await _campaign_progress(ctx.workspace_id, campaign_id)
    return {
        **_doc_to_campaign(updated).model_dump(),
        "progress": progress,
        "pieces_generated_this_run": len(result["new_piece_ids"]),
    }


@router.post("/{campaign_id}/thumbnail", response_model=Campaign)
@limiter.limit("10/minute")
async def upload_campaign_thumbnail(
    request: Request,
    campaign_id: str,
    file: UploadFile = File(...),
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> Campaign:
    """User-uploaded thumbnail only — there's no real image-generation
    pipeline to derive one from (ContentType.IMAGE stays a stub, see
    app/api/v1/image.py). Stored on Cloudinary under the uploading user's
    id, same convention app.shared.storage already documents."""
    campaign = await get_campaigns_collection().find_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    if file.content_type not in ALLOWED_THUMBNAIL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type '{file.content_type}'. Use JPEG, PNG, or WebP.",
        )

    contents = await file.read()
    if len(contents) > MAX_THUMBNAIL_BYTES:
        raise HTTPException(status_code=400, detail="Thumbnail must be 5MB or smaller.")

    try:
        thumbnail_url = await upload_file(contents, UploadContentType.THUMBNAIL, ctx.user_id)
    except Exception as e:
        logger.error("Campaign thumbnail upload failed for %s: %s", campaign_id, e)
        raise HTTPException(status_code=502, detail="Thumbnail upload failed. Try again.")

    await get_campaigns_collection().update_one(
        {"id": campaign_id, "workspace_id": ctx.workspace_id},
        {"$set": {"thumbnail_url": thumbnail_url, "updated_at": datetime.now(timezone.utc)}},
    )
    updated = await get_campaigns_collection().find_one({"id": campaign_id, "workspace_id": ctx.workspace_id})
    return _doc_to_campaign(updated)


class SuggestCampaignTopicsRequest(BaseModel):
    topic_cluster: str
    brand_id: str
    existing_topics: list[str] = []


@router.post("/suggest-topics")
@limiter.limit("20/minute")
async def suggest_topics(
    request: Request,
    body: SuggestCampaignTopicsRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """
    Real "AI Suggestions" step for the campaign runner form — read-only,
    never persists anything, mirrors /text/repurpose/suggest's pattern
    (app.pipelines.text.repurpose_suggest.suggest_repurpose_targets): a
    single cheap structured LLM call the user can accept or ignore, not a
    blocker to filling the form manually.
    """
    brand = await brand_profiles.find_one({"id": body.brand_id, "workspace_id": ctx.workspace_id})
    if not brand:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    brand_context = build_brand_context(brand)
    result = await suggest_campaign_topics(
        topic_cluster=body.topic_cluster,
        brand_context=brand_context,
        existing_topics=body.existing_topics,
    )
    return result
