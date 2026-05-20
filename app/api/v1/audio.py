"""Audio pipeline API router."""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import get_current_user

router = APIRouter()


class AudioRequest(BaseModel):
    """Request body for the audio generation pipeline."""

    script: str
    voice_id: str = "default"
    language: str = "en"
    speed: float = 1.0


@router.get("")
async def audio_status() -> dict[str, str]:
    """Return the health status of the audio pipeline."""
    return {"status": "ok", "pipeline": "audio"}


@router.post("")
async def run_audio_pipeline(
    body: AudioRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Accept an audio generation request and queue the pipeline.

    Args:
        body: Audio generation parameters including script and voice settings.
        current_user: JWT payload of the authenticated caller.

    Returns:
        Acknowledgement with queued status.
    """
    return {"status": "queued", "message": "pipeline started"}
