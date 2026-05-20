"""Publish pipeline API router."""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import get_current_user

router = APIRouter()


class PublishRequest(BaseModel):
    """Request body for the publish pipeline."""

    content_id: str
    platforms: list[str] = ["linkedin"]
    schedule_at: str | None = None
    dry_run: bool = False


@router.get("")
async def publish_status() -> dict[str, str]:
    """Return the health status of the publish pipeline."""
    return {"status": "ok", "pipeline": "publish"}


@router.post("")
async def run_publish_pipeline(
    body: PublishRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Accept a publish request and queue the pipeline.

    Args:
        body: Publish parameters including content ID and target platforms.
        current_user: JWT payload of the authenticated caller.

    Returns:
        Acknowledgement with queued status.
    """
    return {"status": "queued", "message": "pipeline started"}
