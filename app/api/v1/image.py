"""Image pipeline API router."""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import get_current_user

router = APIRouter()


class ImageRequest(BaseModel):
    """Request body for the image generation pipeline."""

    prompt: str
    style: str = "photorealistic"
    dimensions: str = "1024x1024"
    platform: str = "instagram"


@router.get("")
async def image_status() -> dict[str, str]:
    """Return the health status of the image pipeline."""
    return {"status": "ok", "pipeline": "image"}


@router.post("")
async def run_image_pipeline(
    body: ImageRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Accept an image generation request and queue the pipeline.

    Args:
        body: Image generation parameters including prompt, style, and dimensions.
        current_user: JWT payload of the authenticated caller.

    Returns:
        Acknowledgement with queued status.
    """
    return {"status": "queued", "message": "pipeline started"}
