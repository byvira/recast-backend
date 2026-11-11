"""Audio Promotion Bundle: draft posts and quote pictures from the best moments of a recording.

GET  /{audio_asset_id}/promotion/estimate   what it would do and cost, before anything is made or charged
POST /{audio_asset_id}/promotion            start it; the price the member saw must be sent back, so a price that changed stops it

Everything made is a draft for review. Nothing is published or scheduled. The writing and the pictures run as the usual saved runs
(the member can leave the page; progress is in Control Tower and a note lands in the Activity Log).
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.billing import credits
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.db.mongo import audio_assets, brand_profiles
from app.models.text import GenerateTextRequest, InputSourceType, Platform
from app.pipelines.media import promotion

router = APIRouter()
logger = logging.getLogger(__name__)

#: Recordings whose bundle is being started right now. A second click or tab would pay for the same moments twice.
_STARTING: set[str] = set()


class PromotionRequest(BaseModel):
    brand_id: str
    platform: Platform
    count: int = Field(3, ge=1, le=promotion.MAX_MOMENTS)
    with_pictures: bool = True
    # The credits the member was shown. If the price has changed since, nothing is started.
    confirm_credits: int = Field(ge=0)


async def _recording(audio_asset_id: str, ctx: WorkspaceContext) -> dict:
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    return doc


@router.get("/{audio_asset_id}/promotion/estimate")
@limiter.limit("30/minute")
async def promotion_estimate(
    request: Request,
    audio_asset_id: str,
    platform: Platform = Query(...),
    count: int = Query(3, ge=1, le=promotion.MAX_MOMENTS),
    with_pictures: bool = Query(True),
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    doc = await _recording(audio_asset_id, ctx)
    plan = await promotion.estimate(platform.value, count, with_pictures)
    reasons, spendable = await promotion.blockers(ctx.workspace_id, plan["credits"])
    if not doc.get("transcript"):
        reasons.append("This recording has no written-out text yet. Transcribe it first.")
    return {**plan, "credits_left": spendable, "can_start": not reasons, "blockers": reasons}


@router.post("/{audio_asset_id}/promotion", status_code=202)
@limiter.limit("3/minute")
async def start_promotion(
    request: Request,
    audio_asset_id: str,
    body: PromotionRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    from app.api.v1.audio_assets import _generate_clip_suggestions
    from app.api.v1.image_assets import GenerateImageAssetRequest, start_image_generation_run
    from app.shared import jobs

    doc = await _recording(audio_asset_id, ctx)
    if not await brand_profiles.find_one({"id": body.brand_id, "workspace_id": ctx.workspace_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="Brand not found.")
    plan = await promotion.estimate(body.platform.value, body.count, body.with_pictures)
    if body.confirm_credits != plan["credits"]:
        raise HTTPException(status_code=409, detail=f"The price is now {plan['credits']} credits. Look at it again before you start.")
    reasons, _ = await promotion.blockers(ctx.workspace_id, plan["credits"])
    if reasons:
        raise HTTPException(status_code=409, detail=reasons[0])

    key = f"{ctx.workspace_id}:{audio_asset_id}"
    if key in _STARTING:
        raise HTTPException(status_code=409, detail="The bundle for this recording is already starting.")
    _STARTING.add(key)
    try:
        async with credits.spend(ctx.workspace_id, ctx.user_id, "audio_script", step=f"promotion:{audio_asset_id}") as hold:
            suggestions = await _generate_clip_suggestions(doc, ctx.workspace_id, audio_asset_id)
            hold.nothing_made = not suggestions
        if not suggestions:
            raise HTTPException(status_code=422, detail="No moment in this recording stood on its own. Try again, or try a longer recording.")

        title = (doc.get("title") or "this recording").strip()[:80]
        made: list[dict[str, Any]] = []
        for moment in suggestions[: body.count]:
            item: dict[str, Any] = {"quote": moment.quote, "post_run": None, "picture_run": None, "notes": []}
            try:
                post_request = GenerateTextRequest(
                    source_type=InputSourceType.TRANSCRIPT, content=moment.quote, platforms=[body.platform], brand_id=body.brand_id,
                )
                run, _already = await jobs.submit(action_name="text.generate", payload=post_request.model_dump(mode="json"), ctx=ctx)
                item["post_run"] = run.get("id")
            except HTTPException as exc:
                item["notes"].append(f"The post could not be started: {exc.detail}")
            headline = promotion.picture_headline(moment.quote) if body.with_pictures else None
            if body.with_pictures and not headline:
                item["notes"].append("This moment is too long for a picture headline, so only the post was made.")
            if headline:
                try:
                    picture = GenerateImageAssetRequest(
                        title=f"Quote from {title}"[:200], brand_id=body.brand_id, headline=headline,
                        prompt=f"A calm, text-free background that fits this idea: {moment.quote[:200]}",
                    )
                    started = await start_image_generation_run(picture, ctx)
                    item["picture_run"] = started.get("id")
                except HTTPException as exc:
                    item["notes"].append(f"The picture could not be started: {exc.detail}")
            made.append(item)
        return {"audio_asset_id": audio_asset_id, "credits": plan["credits"], "items": made, "published": False}
    finally:
        _STARTING.discard(key)
