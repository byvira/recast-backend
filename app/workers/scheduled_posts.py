"""
Scheduled posts worker.
Runs every minute — finds queued posts and fires publish pipeline.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.notifications import send_templated_email
from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import content_pieces, users, workspaces
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.registry import get_publisher
from app.pipelines.publish.supervisor.alerts import alert_fatal
from app.pipelines.publish.supervisor.classifier import classify_error, ErrorType
from app.pipelines.publish.supervisor.retry import get_retry_delay, should_retry
from app.pipelines.publish.token_store import get_token

logger = logging.getLogger(__name__)


async def _notify_publish_failure(
    *, piece_id: str, platform: str, user_id: str, brand_id: str,
    workspace_id: str, error_message: str, scheduled_at: str = "",
) -> None:
    """Alert ops and email the content owner about a scheduled-publish failure.

    Mirrors the FATAL alert path the synchronous /publish/now endpoint already
    uses (app.api.v1.publish) — this worker previously fired no alert of any
    kind. Never raises; a notification failure must not crash the scheduler.
    """
    try:
        await alert_fatal(
            piece_id=piece_id,
            platform=platform,
            user_id=user_id,
            brand_id=brand_id,
            error_code=None,
            error_message=error_message,
            workspace_id=workspace_id,
        )
    except Exception as e:
        logger.error("alert_fatal failed for piece %s: %s", piece_id, e)

    try:
        owner = await users.find_one({"id": user_id}, {"email": 1})
        ws = await workspaces.find_one({"id": workspace_id}, {"name": 1}) if workspace_id else None
        if owner and owner.get("email"):
            await send_templated_email(
                "scheduled-post-failed",
                owner["email"],
                {
                    "PLATFORM": platform,
                    "WORKSPACE_NAME": (ws or {}).get("name", "your workspace"),
                    "SCHEDULED_AT": scheduled_at,
                    "ERROR_MESSAGE": error_message,
                    "PIECE_ID": piece_id,
                    "DASHBOARD_LINK": f"{settings.FRONTEND_URL}/dashboard",
                },
            )
    except Exception as e:
        logger.error("publish-failure email failed for piece %s: %s", piece_id, e)


@distributed_job_lock("process_scheduled_posts", ttl_seconds=55)
async def process_scheduled_posts() -> None:
    """
    Find all posts scheduled for now or earlier and publish them.
    Called every minute by the scheduler.
    """
    now = datetime.now(timezone.utc).isoformat()

    due_posts = await content_pieces.find({
        "publish_status":       "queued",
        "publish_scheduled_at": {"$lte": now},
        "deleted":              {"$ne": True},
    }).to_list(length=50)

    if not due_posts:
        return

    logger.info("Found %d scheduled posts due for publishing", len(due_posts))

    for piece in due_posts:
        try:
            await _publish_scheduled_piece(piece)
        except Exception as e:
            logger.error(
                "Failed to publish scheduled piece %s: %s",
                piece.get("piece_id"), e,
            )


async def _publish_scheduled_piece(piece: dict) -> None:
    """Publish one scheduled piece."""
    piece_id     = piece["piece_id"]
    platform     = piece.get("publish_target", "linkedin")
    user_id      = piece["user_id"]
    workspace_id = piece.get("workspace_id", "")

    if not workspace_id:
        logger.warning(
            "Piece %s has no workspace_id — skipping (pre-cutover data)", piece_id
        )
        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status": "failed",
                "last_error": "Missing workspace_id",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        await _notify_publish_failure(
            piece_id=piece_id, platform=platform, user_id=user_id,
            brand_id=piece.get("brand_id", ""), workspace_id="",
            error_message="Missing workspace_id",
            scheduled_at=piece.get("publish_scheduled_at", ""),
        )
        return

    # Get token — scoped to the piece's workspace
    token_data = await get_token(workspace_id, platform)
    if not token_data:
        logger.warning(
            "No token for workspace %s platform %s — piece %s skipped",
            workspace_id, platform, piece_id,
        )
        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status": "failed",
                "last_error": f"No {platform} token found",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        await _notify_publish_failure(
            piece_id=piece_id, platform=platform, user_id=user_id,
            brand_id=piece.get("brand_id", ""), workspace_id=workspace_id,
            error_message=f"No {platform} token found",
            scheduled_at=piece.get("publish_scheduled_at", ""),
        )
        return

    # Get publisher
    try:
        publisher = get_publisher(platform)
    except ValueError:
        logger.error("No publisher for platform %s", platform)
        return

    # Build request
    pub_request = PublishRequest(
        piece_id=piece_id,
        workspace_id=workspace_id,
        user_id=user_id,
        brand_id=piece["brand_id"],
        platform=platform,
        content=piece["content"],
    )
    pub_request.platform_user_id = token_data.get("platform_user_id", "")

    # Mark as publishing
    await content_pieces.update_one(
        {"piece_id": piece_id},
        {"$set": {
            "publish_status": "publishing",
            "updated_at": datetime.now(timezone.utc),
        }},
    )

    result = await publisher.publish(pub_request, token_data["access_token"])

    if result.success:
        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status":    "published",
                "platform_post_id":  result.platform_post_id,
                "platform_post_url": result.platform_post_url,
                "updated_at":        datetime.now(timezone.utc),
            }},
        )
        try:
            from app.shared.governance_events import emit_content_published
            emit_content_published(
                workspace_id, pipeline_type=piece.get("pipeline_type", "text"),
                actor_user_id=user_id, actor_role="",
                content_id=piece_id, target=platform,
                external_url=result.platform_post_url or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("emit content.published failed for %s: %s", piece_id, exc)
        logger.info("Scheduled piece %s published to %s", piece_id, platform)
    else:
        error_type = classify_error(result.error_code or 500, result.error_message or "")
        attempt = piece.get("publish_attempts", 0)

        # AUTH errors can't be retried without the user reconnecting the
        # platform — same as /publish/now's own AUTH branch — so those (and
        # anything past MAX_RETRIES) fail immediately below. Everything else
        # (TRANSIENT/FIXABLE/FATAL-but-retryable per should_retry) gets
        # requeued instead of permanently failing on the first error — this
        # worker previously had no retry or requeue logic at all, unlike
        # /publish/now's synchronous retry loop.
        if error_type != ErrorType.AUTH and should_retry(error_type, attempt):
            delay = get_retry_delay(error_type, attempt) or 60
            next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            await content_pieces.update_one(
                {"piece_id": piece_id},
                {
                    "$set": {
                        "publish_status": "queued",
                        "publish_scheduled_at": next_at.isoformat(),
                        "last_error": result.error_message,
                        "updated_at": datetime.now(timezone.utc),
                    },
                    "$inc": {"publish_attempts": 1},
                },
            )
            logger.info(
                "Scheduled piece %s failed on %s (attempt %d, %s) — requeued for retry at %s",
                piece_id, platform, attempt + 1, error_type.value, next_at.isoformat(),
            )
            return

        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status": "failed",
                "last_error":     result.error_message,
                "updated_at":     datetime.now(timezone.utc),
            }},
        )
        await _notify_publish_failure(
            piece_id=piece_id, platform=platform, user_id=user_id,
            brand_id=piece["brand_id"], workspace_id=workspace_id,
            error_message=result.error_message or "Unknown error",
            scheduled_at=piece.get("publish_scheduled_at", ""),
        )
        logger.error(
            "Scheduled piece %s failed on %s: %s",
            piece_id, platform, result.error_message,
        )