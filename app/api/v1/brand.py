"""Brand pipeline API router."""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import get_current_user

router = APIRouter()


class BrandRequest(BaseModel):
    """Request body for the brand processing pipeline."""

    content: str
    brand_id: str
    enforce_voice: bool = True
    check_blacklist: bool = True


@router.get("")
async def brand_status() -> dict[str, str]:
    """Return the health status of the brand pipeline."""
    return {"status": "ok", "pipeline": "brand"}


@router.post("")
async def run_brand_pipeline(
    body: BrandRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Accept a brand processing request and queue the pipeline.

    Args:
        body: Brand parameters including content and brand profile ID.
        current_user: JWT payload of the authenticated caller.

    Returns:
        Acknowledgement with queued status.
    """
    return {"status": "queued", "message": "pipeline started"}
