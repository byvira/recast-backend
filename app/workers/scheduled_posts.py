"""
Scheduled posts worker.
Runs every minute — finds queued posts and fires publish pipeline.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.db.mongo import content_pieces
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.registry import get_publisher
from app.pipelines.publish.token_store import get_token

logger = logging.getLogger(__name__)


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
    piece_id = piece["piece_id"]
    platform = piece.get("publish_target", "linkedin")
    user_id  = piece["user_id"]

    # Get token
    token_data = await get_token(user_id, platform)
    if not token_data:
        logger.warning(
            "No token for user %s platform %s — piece %s skipped",
            user_id, platform, piece_id,
        )
        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status": "failed",
                "last_error": f"No {platform} token found",
                "updated_at": datetime.now(timezone.utc),
            }},
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
        logger.info("Scheduled piece %s published to %s", piece_id, platform)
    else:
        await content_pieces.update_one(
            {"piece_id": piece_id},
            {"$set": {
                "publish_status": "failed",
                "last_error":     result.error_message,
                "updated_at":     datetime.now(timezone.utc),
            }},
        )
        logger.error(
            "Scheduled piece %s failed on %s: %s",
            piece_id, platform, result.error_message,
        )