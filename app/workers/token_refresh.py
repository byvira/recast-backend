"""
Token refresh worker.
Runs every 24 hours — refreshes tokens expiring within 7 days.
"""

import logging
from datetime import datetime, timezone, timedelta

from app.db.mongo import users
from app.pipelines.publish.token_store import (
    get_token,
    save_token,
    decrypt_token,
)
from app.pipelines.publish.registry import get_publisher

logger = logging.getLogger(__name__)


async def refresh_expiring_tokens() -> None:
    """
    Find all tokens expiring within 7 days and refresh them.
    Called every 24 hours by the scheduler.
    """
    threshold = datetime.now(timezone.utc) + timedelta(days=7)

    all_users = await users.find(
        {"social_accounts": {"$exists": True, "$ne": []}}
    ).to_list(length=1000)

    refreshed = 0
    failed    = 0

    for user in all_users:
        for account in user.get("social_accounts", []):
            if not account.get("is_active"):
                continue

            expires_at = account.get("expires_at")
            if not expires_at:
                continue

            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)

            if expires_at > threshold:
                continue

            # Token expiring soon — refresh it
            platform = account["platform"]
            user_id  = user["id"]

            try:
                publisher = get_publisher(platform)
            except ValueError:
                continue

            try:
                encrypted_refresh = account.get("refresh_token")
                if not encrypted_refresh:
                    logger.warning(
                        "No refresh token for user %s platform %s",
                        user_id, platform,
                    )
                    continue

                refresh_token_value = decrypt_token(encrypted_refresh)
                new_token = await publisher.refresh_token(refresh_token_value)

                # Get current token data to preserve unchanged fields
                current = await get_token(user_id, platform)
                if not current:
                    continue

                await save_token(
                    user_id=user_id,
                    platform=platform,
                    access_token=new_token["access_token"],
                    refresh_token=refresh_token_value,
                    expires_at=new_token["expires_at"],
                    platform_user_id=current.get("platform_user_id", ""),
                    username=current.get("username", ""),
                )

                refreshed += 1
                logger.info(
                    "Refreshed token for user %s platform %s",
                    user_id, platform,
                )

            except Exception as e:
                failed += 1
                logger.error(
                    "Token refresh failed for user %s platform %s: %s",
                    user_id, platform, e,
                )

    logger.info(
        "Token refresh complete — refreshed: %d, failed: %d",
        refreshed, failed,
    )