"""
Publish endpoints — publish now, schedule, cancel, status.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.db.mongo import content_pieces
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.registry import get_publisher
from app.pipelines.publish.token_store import get_token
from app.pipelines.publish.supervisor.alerts import alert_fatal, save_incident
from app.pipelines.publish.supervisor.classifier import classify_error, ErrorType
from app.pipelines.publish.supervisor.retry import should_retry, get_retry_delay
from app.pipelines.publish.supervisor.fixer import fix_content
from app.pipelines.publish.validators import validate_for_platform

router = APIRouter()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class PublishNowRequest(BaseModel):
    piece_id: str
    platform: str


class ScheduleRequest(BaseModel):
    piece_id: str
    platform: str
    scheduled_at: str   # ISO datetime string


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _get_verified_piece(
    piece_id: str,
    user_id: str,
) -> dict:
    """Fetch piece and verify ownership."""
    piece = await content_pieces.find_one(
        {"piece_id": piece_id, "deleted": {"$ne": True}}
    )
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    if piece["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    return piece


async def _update_piece_status(
    piece_id: str,
    status: str,
    platform_post_id: Optional[str] = None,
    platform_post_url: Optional[str] = None,
    error_message: Optional[str] = None,
    increment_attempts: bool = False,
) -> None:
    """Update publish status fields on a piece."""
    updates: dict = {
        "publish_status": status,
        "updated_at": datetime.now(timezone.utc),
    }
    if platform_post_id:
        updates["platform_post_id"] = platform_post_id
    if platform_post_url:
        updates["platform_post_url"] = platform_post_url
    if error_message:
        updates["last_error"] = error_message
    if increment_attempts:
        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$inc": {"publish_attempts": 1}, "$set": updates},
        )
        return

    await content_pieces.update_one(
        {"piece_id": piece_id},
        {"$set": updates},
    )


# ─────────────────────────────────────────────────────────────────────────────
# PUBLISH NOW
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/now")
@limiter.limit("10/minute")
async def publish_now(
    request: Request,
    body: PublishNowRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """
    Publish a piece immediately to a platform.
    Runs supervisor logic — auto-retry on transient errors,
    auto-fix on fixable errors, alert on fatal errors.
    """
    piece = await _get_verified_piece(body.piece_id, current_user["id"])

    # Check platform token exists
    token_data = await get_token(current_user["id"], body.platform)
    if not token_data:
        raise HTTPException(
            status_code=400,
            detail=f"{body.platform} is not connected. "
                   f"Connect at /api/v1/oauth/{body.platform}/connect",
        )

    # Get publisher
    try:
        publisher = get_publisher(body.platform)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Platform '{body.platform}' not supported.",
        )

    # Build publish request
    pub_request = PublishRequest(
        piece_id=body.piece_id,
        user_id=current_user["id"],
        brand_id=piece["brand_id"],
        platform=body.platform,
        content=piece["content"],
    )
    # Inject platform_user_id for LinkedIn UGC author field
    pub_request.platform_user_id = token_data.get("platform_user_id", "")

    # Update status to publishing
    await _update_piece_status(body.piece_id, "publishing")

    content    = piece["content"]
    attempt    = 0
    max_attempts = 3

    while attempt < max_attempts:
        pub_request.content = content
        result = await publisher.publish(pub_request, token_data["access_token"])

        if result.success:
            await _update_piece_status(
                body.piece_id,
                "published",
                platform_post_id=result.platform_post_id,
                platform_post_url=result.platform_post_url,
                increment_attempts=True,
            )
            return {
                "success":          True,
                "piece_id":         body.piece_id,
                "platform":         body.platform,
                "platform_post_id": result.platform_post_id,
                "platform_post_url": result.platform_post_url,
                "attempts":         attempt + 1,
            }

        # Classify error
        error_type = classify_error(
            result.error_code or 500,
            result.error_message or "",
        )

        await save_incident(
            piece_id=body.piece_id,
            platform=body.platform,
            user_id=current_user["id"],
            brand_id=piece["brand_id"],
            error_type=error_type.value,
            error_code=result.error_code,
            error_message=result.error_message or "",
            retry_count=attempt,
            retry_at=None,
        )

        # Handle AUTH — token needs refresh or reconnect
        if error_type == ErrorType.AUTH:
            await _update_piece_status(
                body.piece_id, "failed",
                error_message=result.error_message,
                increment_attempts=True,
            )
            raise HTTPException(
                status_code=401,
                detail=f"{body.platform} token expired or revoked. "
                       f"Reconnect at /api/v1/oauth/{body.platform}/connect",
            )

        # Handle FIXABLE — auto-fix content and retry once
        if error_type == ErrorType.FIXABLE:
            fixed, new_content = fix_content(
                body.platform, content, result.error_message or ""
            )
            if fixed:
                content = new_content
                attempt += 1
                continue
            else:
                await _update_piece_status(
                    body.piece_id, "flagged",
                    error_message=result.error_message,
                    increment_attempts=True,
                )
                return {
                    "success":  False,
                    "piece_id": body.piece_id,
                    "platform": body.platform,
                    "status":   "flagged",
                    "reason":   result.error_message,
                }

        # Handle FATAL — alert and stop
        if error_type == ErrorType.FATAL:
            await alert_fatal(
                piece_id=body.piece_id,
                platform=body.platform,
                user_id=current_user["id"],
                brand_id=piece["brand_id"],
                error_code=result.error_code,
                error_message=result.error_message or "",
            )
            await _update_piece_status(
                body.piece_id, "flagged",
                error_message=result.error_message,
                increment_attempts=True,
            )
            return {
                "success":  False,
                "piece_id": body.piece_id,
                "platform": body.platform,
                "status":   "flagged",
                "reason":   result.error_message,
            }

        # Handle TRANSIENT — backoff and retry
        if should_retry(error_type, attempt):
            import asyncio
            delay = get_retry_delay(error_type, attempt)
            if delay > 0:
                logger.info(
                    "Transient error on %s attempt %d — retrying in %ds",
                    body.platform, attempt + 1, delay,
                )
                await asyncio.sleep(min(delay, 10))  # cap at 10s for API response
            attempt += 1
            continue

        break

    # All attempts exhausted
    await _update_piece_status(
        body.piece_id, "failed",
        error_message="All retry attempts exhausted",
        increment_attempts=True,
    )
    return {
        "success":  False,
        "piece_id": body.piece_id,
        "platform": body.platform,
        "status":   "failed",
        "reason":   "All retry attempts exhausted",
        "attempts": attempt,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULE
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/schedule")
@limiter.limit("20/minute")
async def schedule_post(
    request: Request,
    body: ScheduleRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """
    Schedule a piece for publishing at a future time.
    The scheduled_posts worker fires the publish at the right time.
    """
    piece = await _get_verified_piece(body.piece_id, current_user["id"])

    # Validate token exists
    token_data = await get_token(current_user["id"], body.platform)
    if not token_data:
        raise HTTPException(
            status_code=400,
            detail=f"{body.platform} is not connected.",
        )

    # Validate content before scheduling
    is_valid, issues = validate_for_platform(body.platform, piece["content"])
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Content validation failed: {'; '.join(issues)}",
        )

    now = datetime.now(timezone.utc)
    await content_pieces.update_one(
        {"piece_id": body.piece_id},
        {"$set": {
            "publish_status":       "queued",
            "publish_scheduled_at": body.scheduled_at,
            "publish_target":       body.platform,
            "updated_at":           now,
        }},
    )

    return {
        "piece_id":     body.piece_id,
        "platform":     body.platform,
        "scheduled_at": body.scheduled_at,
        "status":       "queued",
    }


# ─────────────────────────────────────────────────────────────────────────────
# CANCEL SCHEDULED
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{piece_id}")
@limiter.limit("20/minute")
async def cancel_scheduled(
    request: Request,
    piece_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Cancel a scheduled post. Only works if status is queued."""
    piece = await _get_verified_piece(piece_id, current_user["id"])

    if piece.get("publish_status") != "queued":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel — post is '{piece.get('publish_status')}' not 'queued'.",
        )

    await content_pieces.update_one(
        {"piece_id": piece_id},
        {"$set": {
            "publish_status":       "pending",
            "publish_scheduled_at": None,
            "updated_at":           datetime.now(timezone.utc),
        }},
    )

    return {"piece_id": piece_id, "cancelled": True}


# ─────────────────────────────────────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{piece_id}/status")
@limiter.limit("60/minute")
async def get_publish_status(
    request: Request,
    piece_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Get the current publish status of a piece."""
    piece = await _get_verified_piece(piece_id, current_user["id"])

    return {
        "piece_id":             piece_id,
        "publish_status":       piece.get("publish_status", "pending"),
        "publish_scheduled_at": piece.get("publish_scheduled_at"),
        "publish_target":       piece.get("publish_target"),
        "platform_post_id":     piece.get("platform_post_id"),
        "platform_post_url":    piece.get("platform_post_url"),
        "publish_attempts":     piece.get("publish_attempts", 0),
        "last_error":           piece.get("last_error"),
    }