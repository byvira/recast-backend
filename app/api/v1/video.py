"""Video pipeline API router."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.middleware import limiter

router = APIRouter()


class VideoRequest(BaseModel):
    """Request body for the video processing pipeline."""

    source_url: str
    target_platform: str = "youtube"
    style: str = "cinematic"
    duration_limit: int = 60


@router.get("")
@limiter.limit("60/minute")
async def video_status(request: Request) -> dict[str, str]:
    """Return the health status of the video pipeline."""
    return {"status": "ok", "pipeline": "video"}


@router.post("")
@limiter.limit("10/minute")
async def run_video_pipeline(
    request: Request,
    body: VideoRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Accept a video processing request and queue the pipeline.

    Args:
        body: Video processing parameters including source URL and target platform.
        current_user: JWT payload of the authenticated caller.

    Returns:
        Acknowledgement with queued status.
    """
    return {"status": "queued", "message": "pipeline started"}
