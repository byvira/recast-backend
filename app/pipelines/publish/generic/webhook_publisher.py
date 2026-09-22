"""Generic publisher for token_webhook platforms (Telegram, Discord, Slack,
Microsoft Teams, Google Chat, ...). POSTs to a configured webhook URL — no
per-platform Python code needed, see app/pipelines/publish/generic/__init__.py.
"""

import logging

import httpx

from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.platform_config_store import get_platform_config_secrets

logger = logging.getLogger(__name__)


class WebhookPublisher:
    """Not a PlatformPublisher subclass — that contract is OAuth-shaped
    (build_auth_url/exchange_token/refresh_token), which doesn't apply to a
    bot-token/webhook platform with no user consent flow at all."""

    async def publish(
        self,
        workspace_id: str,
        platform: str,
        content: str,
        fields: dict,
        media_urls: list[str] | None = None,
        piece_id: str = "",
    ) -> PublishResult:
        secrets = await get_platform_config_secrets(workspace_id, platform)
        webhook_url = secrets.get("webhook_url")
        if not webhook_url:
            return PublishResult(
                success=False,
                platform=platform,
                piece_id=piece_id,
                error_type="FIXABLE",
                error_message="No webhook_url configured for this platform in the Ops Dashboard.",
            )

        payload = {"content": content, "media_urls": media_urls or [], **fields}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(webhook_url, json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Webhook publish failed — platform=%s status=%s", platform, status)
            return PublishResult(
                success=False,
                platform=platform,
                piece_id=piece_id,
                error_type="TRANSIENT" if status >= 500 else "FIXABLE",
                error_code=status,
                error_message=str(exc),
            )
        except httpx.HTTPError as exc:
            logger.warning("Webhook publish failed — platform=%s error=%s", platform, exc)
            return PublishResult(
                success=False,
                platform=platform,
                piece_id=piece_id,
                error_type="TRANSIENT",
                error_message=str(exc),
            )

        logger.info("Webhook publish succeeded — workspace=%s platform=%s", workspace_id, platform)
        return PublishResult(success=True, platform=platform, piece_id=piece_id)
