"""
Instagram publisher via Meta Graph API.
Requires Instagram Business or Creator account linked to Facebook Page.
Two-step process: create container → publish container.
Image is required for standard posts.
Text-only posts use the reels or carousel workaround.
"""

import logging
import httpx

from app.pipelines.publish.base import (
    PlatformPublisher,
    PublishRequest,
    PublishResult,
)
from app.pipelines.publish.validators import validate_instagram
from app.pipelines.publish.meta.oauth import (
    build_auth_url,
    exchange_code,
    refresh_meta_token,
)
from app.pipelines.publish.supervisor.classifier import classify_error

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v19.0"


class InstagramPublisher(PlatformPublisher):

    def build_auth_url(self, state: str) -> str:
        return build_auth_url(state, platform="instagram")

    async def exchange_token(self, code: str) -> dict:
        return await exchange_code(code, platform="instagram")

    async def refresh_token(self, refresh_token: str) -> dict:
        # Meta refreshes using the access token itself not refresh token
        return await refresh_meta_token(refresh_token)

    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        return validate_instagram(content)

    async def publish(
        self,
        request: PublishRequest,
        access_token: str,
    ) -> PublishResult:
        """
        Publish to Instagram.
        Uses image_url if provided, otherwise attempts text-only via
        the caption-only container (requires image in practice).
        """
        is_valid, issues = self.validate_content(request.content)
        if not is_valid:
            return PublishResult(
                success=False,
                platform="instagram",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=f"Content validation failed: {'; '.join(issues)}",
            )

        ig_user_id = getattr(request, "ig_user_id", None) or request.platform_user_id
        image_url  = (request.media_urls or [None])[0]

        if not image_url:
            return PublishResult(
                success=False,
                platform="instagram",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=(
                    "Instagram requires an image. "
                    "Provide media_urls with at least one image URL."
                ),
            )

        try:
            async with httpx.AsyncClient() as client:
                # Step 1 — Create media container
                container_response = await client.post(
                    f"{GRAPH_BASE}/{ig_user_id}/media",
                    params={
                        "image_url":    image_url,
                        "caption":      request.content,
                        "access_token": access_token,
                    },
                )

                if container_response.status_code != 200:
                    error_data    = container_response.json()
                    error_message = error_data.get("error", {}).get("message", container_response.text)
                    error_type    = classify_error(container_response.status_code, error_message)
                    return PublishResult(
                        success=False,
                        platform="instagram",
                        piece_id=request.piece_id,
                        error_type=error_type.value,
                        error_code=container_response.status_code,
                        error_message=error_message,
                    )

                container_id = container_response.json()["id"]

                # Step 2 — Publish the container
                publish_response = await client.post(
                    f"{GRAPH_BASE}/{ig_user_id}/media_publish",
                    params={
                        "creation_id":  container_id,
                        "access_token": access_token,
                    },
                )

                if publish_response.status_code == 200:
                    post_id  = publish_response.json()["id"]
                    post_url = f"https://www.instagram.com/p/{post_id}/"
                    logger.info(
                        "Instagram post published: %s for piece %s",
                        post_id, request.piece_id,
                    )
                    return PublishResult(
                        success=True,
                        platform="instagram",
                        piece_id=request.piece_id,
                        platform_post_id=post_id,
                        platform_post_url=post_url,
                    )

                error_data    = publish_response.json()
                error_message = error_data.get("error", {}).get("message", publish_response.text)
                error_type    = classify_error(publish_response.status_code, error_message)

                return PublishResult(
                    success=False,
                    platform="instagram",
                    piece_id=request.piece_id,
                    error_type=error_type.value,
                    error_code=publish_response.status_code,
                    error_message=error_message,
                )

        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                platform="instagram",
                piece_id=request.piece_id,
                error_type="TRANSIENT",
                error_code=408,
                error_message="Request timed out",
                retry_after=5,
            )
        except Exception as exc:
            logger.error("Instagram publisher error: %s", exc)
            return PublishResult(
                success=False,
                platform="instagram",
                piece_id=request.piece_id,
                error_type="FATAL",
                error_code=500,
                error_message=str(exc),
            )