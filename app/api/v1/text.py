"""Text pipeline API router."""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import get_current_user

router = APIRouter()


class TextRequest(BaseModel):
    """Request body for the text generation pipeline."""

    topic: str
    platform: str = "linkedin"
    tone: str = "professional"
    word_count: int = 500


@router.get("")
async def text_status() -> dict[str, str]:
    """Return the health status of the text pipeline."""
    return {"status": "ok", "pipeline": "text"}


@router.post("")
async def run_text_pipeline(
    body: TextRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Accept a text generation request and queue the pipeline.

    Args:
        body: Text generation parameters including topic, platform, and tone.
        current_user: JWT payload of the authenticated caller.

    Returns:
        Acknowledgement with queued status.
    """
    return {"status": "queued", "message": "pipeline started"}
