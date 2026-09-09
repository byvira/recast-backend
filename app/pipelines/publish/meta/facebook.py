"""
Facebook publisher via Meta Graph API.
Posts to Facebook Pages — personal profiles not supported via API.
User must manage at least one Facebook Page.
Page access token used for posting (not user token).
"""

import logging
import httpx

from app.pipelines.publish.base import (
    PlatformPublisher,
    PublishRequest,
    PublishResult,
)
from app.pipelines.publish.validators import validate_facebook
from app.pipelines.publish.meta.oauth import (
    build_auth_url,
    exchange_code,
    refresh_meta_token,
)
from app.pipelines.publish.supervisor.classifier import classify_error

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v19.0"


class FacebookPublisher(PlatformPublisher):

    def build_auth_url(self, state: str) -> str:
        return build_auth_url(state, platform="facebook")

    async def exchange_token(self, code: str) -> dict:
        return await exchange_code(code, platform="facebook")

    async def refresh_token(self, refresh_token: str) -> dict:
        return await refresh_meta_token(refresh_token)

    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        return validate_facebook(content)

    async def publish(
        self,
        request: PublishRequest,
        access_token: str,
    ) -> PublishResult:
        """
        Publish a post to a Facebook Page.
        platform_user_id should be the Page ID.
        access_token should be the Page access token.
        """
        is_valid, issues = self.validate_content(request.content)
        if not is_valid:
            return PublishResult(
                success=False,
                platform="facebook",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=f"Content validation failed: {'; '.join(issues)}",
            )

        page_id = request.platform_user_id

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{GRAPH_BASE}/{page_id}/feed",
                    params={
                        "message":      request.content,
                        "access_token": access_token,
                    },
                )

                if response.status_code == 200:
                    post_id  = response.json()["id"]
                    post_url = f"https://www.facebook.com/{post_id.replace('_', '/posts/')}"
                    logger.info(
                        "Facebook post published: %s for piece %s",
                        post_id, request.piece_id,
                    )
                    return PublishResult(
                        success=True,
                        platform="facebook",
                        piece_id=request.piece_id,
                        platform_post_id=post_id,
                        platform_post_url=post_url,
                    )

                error_data    = response.json()
                error_message = error_data.get("error", {}).get("message", response.text)
                error_type    = classify_error(response.status_code, error_message)

                logger.error(
                    "Facebook publish failed: %d %s for piece %s",
                    response.status_code, error_message, request.piece_id,
                )

                return PublishResult(
                    success=False,
                    platform="facebook",
                    piece_id=request.piece_id,
                    error_type=error_type.value,
                    error_code=response.status_code,
                    error_message=error_message,
                )

        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                platform="facebook",
                piece_id=request.piece_id,
                error_type="TRANSIENT",
                error_code=408,
                error_message="Request timed out",
                retry_after=5,
            )
        except Exception as exc:
            logger.error("Facebook publisher error: %s", exc)
            return PublishResult(
                success=False,
                platform="facebook",
                piece_id=request.piece_id,
                error_type="FATAL",
                error_code=500,
                error_message=str(exc),
            )