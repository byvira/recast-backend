"""
Supervisor alerting.
Fires on FATAL errors and persistent failures.
Writes to MongoDB incidents collection, Slack, and email.
All functions are non-fatal — never raise exceptions.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


async def save_incident(
    piece_id: str,
    platform: str,
    user_id: str,
    brand_id: str,
    error_type: str,
    error_code: Optional[int],
    error_message: str,
    retry_count: int,
    retry_at,
    trace: str = "",
    workspace_id: str = "",
) -> None:
    """Save a publish incident to MongoDB for audit trail."""
    try:
        from app.db.mongo import get_db
        db = get_db()
        incidents = db["publish_incidents"]

        await incidents.insert_one({
            "piece_id":      piece_id,
            "platform":      platform,
            "workspace_id":  workspace_id,
            "user_id":       user_id,
            "brand_id":      brand_id,
            "error_type":    error_type,
            "error_code":    error_code,
            "error_message": error_message,
            "retry_count":   retry_count,
            "retry_at":      retry_at,
            "resolved":      False,
            "trace":         trace,
            "created_at":    datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.error("Failed to save incident to MongoDB: %s", e)


async def send_slack_alert(
    piece_id: str,
    platform: str,
    error_type: str,
    error_message: str,
    user_id: str,
) -> None:
    """Send alert to Slack. Skips silently if webhook URL not configured."""
    try:
        from app.core.config import settings
        webhook_url = getattr(settings, "SLACK_WEBHOOK_URL", "")
        if not webhook_url:
            logger.debug(
                "SLACK_WEBHOOK_URL not configured — skipping Slack alert "
                "for piece %s platform %s", piece_id, platform
            )
            return

        import httpx
        payload = {
            "text": (
                f"🚨 *Publish {error_type} failure*\n"
                f"Platform: {platform}\n"
                f"Piece: {piece_id}\n"
                f"User: {user_id}\n"
                f"Error: {error_message}"
            )
        }
        async with httpx.AsyncClient() as client:
            await client.post(webhook_url, json=payload, timeout=5.0)

    except Exception as e:
        logger.error("Failed to send Slack alert: %s", e)


async def alert_fatal(
    piece_id: str,
    platform: str,
    user_id: str,
    brand_id: str,
    error_code: Optional[int],
    error_message: str,
    trace: str = "",
    workspace_id: str = "",
) -> None:
    """
    Full alert pipeline for FATAL errors.
    Saves incident to MongoDB + fires Slack notification.
    Never raises — all failures logged and swallowed.
    """
    await save_incident(
        piece_id=piece_id,
        platform=platform,
        user_id=user_id,
        brand_id=brand_id,
        error_type="FATAL",
        error_code=error_code,
        error_message=error_message,
        retry_count=0,
        retry_at=None,
        trace=trace,
        workspace_id=workspace_id,
    )
    await send_slack_alert(
        piece_id=piece_id,
        platform=platform,
        error_type="FATAL",
        error_message=error_message,
        user_id=user_id,
    )