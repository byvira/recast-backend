"""
Token refresh worker.
Runs every 24 hours — refreshes workspace connections expiring within 7 days.
"""

import logging
from datetime import datetime, timezone, timedelta

from app.db.mongo import workspace_connections
from app.pipelines.publish.token_store import (
    save_token,
    decrypt_token,
)
from app.pipelines.publish.registry import get_publisher

logger = logging.getLogger(__name__)


async def refresh_expiring_tokens() -> None:
    """
    Find all workspace connections expiring within 7 days and refresh them.
    Called every 24 hours by the scheduler.
    """
    threshold = datetime.now(timezone.utc) + timedelta(days=7)

    connections = await workspace_connections.find(
        {"is_active": True}
    ).to_list(length=5000)

    refreshed = 0
    failed    = 0

    for account in connections:
        expires_at = account.get("expires_at")
        if not expires_at:
            continue

        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)

        if expires_at > threshold:
            continue

        platform     = account["platform"]
        workspace_id = account["workspace_id"]

        try:
            publisher = get_publisher(platform)
        except ValueError:
            continue

        try:
            encrypted_refresh = account.get("refresh_token")
            if not encrypted_refresh:
                logger.warning(
                    "No refresh token for workspace %s platform %s",
                    workspace_id, platform,
                )
                continue

            refresh_token_value = decrypt_token(encrypted_refresh)
            new_token = await publisher.refresh_token(refresh_token_value)

            await save_token(
                workspace_id=workspace_id,
                platform=platform,
                access_token=new_token["access_token"],
                refresh_token=refresh_token_value,
                expires_at=new_token["expires_at"],
                platform_user_id=account.get("platform_user_id", ""),
                username=account.get("username", ""),
                connected_by=account.get("connected_by", ""),
            )

            refreshed += 1
            logger.info(
                "Refreshed token for workspace %s platform %s",
                workspace_id, platform,
            )

        except Exception as e:
            failed += 1
            logger.error(
                "Token refresh failed for workspace %s platform %s: %s",
                workspace_id, platform, e,
            )

    logger.info(
        "Token refresh complete — refreshed: %d, failed: %d",
        refreshed, failed,
    )
