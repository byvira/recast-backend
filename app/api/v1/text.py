"""Text pipeline API routes — generate, repurpose, batch."""

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.db.mongo import brand_profiles
from app.models.text import (
    BatchGenerateRequest, GenerateTextRequest,
    RepurposeRequest, TextPipelineResult,
    ContentIntent
)
from app.pipelines.text.orchestrator import run_batch_pipeline, run_text_pipeline

router = APIRouter()


async def _get_verified_brand(brand_id: str, user_id: str) -> dict:
    """Fetch brand profile and verify ownership and completion."""
    brand = await brand_profiles.find_one({"id": brand_id, "user_id": user_id})
    if not brand:
        raise HTTPException(status_code=404, detail="Brand profile not found.")
    if not brand.get("is_complete"):
        raise HTTPException(status_code=400, detail="Brand profile is not complete. Finish onboarding first.")
    return brand


@router.post("/generate", response_model=TextPipelineResult)
@limiter.limit("20/minute")
async def generate_text_content(
    request: Request,
    body: GenerateTextRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> TextPipelineResult:
    """
    Generate content for one or more platforms from any input.
    Supports all four frontend input modes: write, prompt, url, repurpose.
    Applies all extras toggles, goal, tone override, and scheduling.
    """
    await _get_verified_brand(body.brand_id, current_user["id"])

    if body.batch_mode:
        # Route batch mode to batch pipeline
        try:
            results = await run_batch_pipeline(
                topic_cluster=body.content,
                platforms=body.platforms,
                brand_id=body.brand_id,
                user_id=current_user["id"],
                extras=body.extras,
                days=body.batch_days,
                detected_intent=body.detected_intent
            )
            # Return first result for response model compatibility
            # Frontend handles list via /batch endpoint
            return results[0] if results else TextPipelineResult(
                session_id="empty", user_id=current_user["id"], brand_id=body.brand_id,
                pieces=[], source_type=body.source_type,
                created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Batch generation error: {str(e)}")

    try:
        result = await run_text_pipeline(
            source_type=body.source_type,
            content=body.content,
            platforms=body.platforms,
            brand_id=body.brand_id,
            user_id=current_user["id"],
            extras=body.extras,
            goal=body.goal,
            tone=body.tone,
            intent=body.intent,
            language=body.language,
            schedule_mode=body.schedule_mode.value,
            scheduled_at=body.scheduled_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {str(e)}")

    return result


@router.post("/repurpose", response_model=TextPipelineResult)
@limiter.limit("20/minute")
async def repurpose_content(
    request: Request,
    body: RepurposeRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> TextPipelineResult:
    """
    Repurpose existing content from one platform format to others.
    Maps to repurpose tab in ConfigPanel InputTabs.
    Brand voice is always re-applied — never copy-paste.
    """
    await _get_verified_brand(body.brand_id, current_user["id"])

    try:
        from app.models.text import InputSourceType
        result = await run_text_pipeline(
            source_type=InputSourceType.TEXT,
            content=body.source_content,
            platforms=body.target_platforms,
            brand_id=body.brand_id,
            user_id=current_user["id"],
            extras=body.extras,
            goal=body.goal,
            tone=body.tone,
            source_platform=body.source_platform,
            is_repurpose=True,
            intent=ContentIntent.AUTO,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Repurpose error: {str(e)}")

    return result


@router.post("/batch", response_model=list[TextPipelineResult])
@limiter.limit("5/minute")
async def batch_generate(
    request: Request,
    body: BatchGenerateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[TextPipelineResult]:
    """
    Generate a full week of content from a single topic cluster.
    Maps to batchMode toggle in ConfigPanel BatchModeToggle.
    Rate limited to 5/minute — this is an expensive operation.
    """
    await _get_verified_brand(body.brand_id, current_user["id"])

    try:
        results = await run_batch_pipeline(
            topic_cluster=body.topic_cluster,
            platforms=body.platforms,
            brand_id=body.brand_id,
            user_id=current_user["id"],
            extras=body.extras,
            days=body.days,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch error: {str(e)}")

    return results
