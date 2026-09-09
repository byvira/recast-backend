"""
LinkedIn publisher.
Posts text content to LinkedIn on behalf of a user.
Implements the PlatformPublisher abstract contract.
"""

import logging
import httpx

from app.pipelines.publish.base import (
    PlatformPublisher,
    PublishRequest,
    PublishResult,
)
from app.pipelines.publish.validators import validate_linkedin
from app.pipelines.publish.linkedin.oauth import (
    build_auth_url,
    exchange_code,
    refresh_access_token,
)
from app.pipelines.publish.supervisor.classifier import classify_error

logger = logging.getLogger(__name__)

LINKEDIN_UGC_URL = "https://api.linkedin.com/v2/ugcPosts"


class LinkedInPublisher(PlatformPublisher):

    def build_auth_url(self, state: str) -> str:
        return build_auth_url(state)

    async def exchange_token(self, code: str) -> dict:
        return await exchange_code(code)

    async def refresh_token(self, refresh_token: str) -> dict:
        return await refresh_access_token(refresh_token)

    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        return validate_linkedin(content)

    async def publish(
        self,
        request: PublishRequest,
        access_token: str,
    ) -> PublishResult:
        """
        Publish a text post to LinkedIn using UGC Posts API.
        """
        is_valid, issues = self.validate_content(request.content)
        if not is_valid:
            return PublishResult(
                success=False,
                platform="linkedin",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=f"Content validation failed: {'; '.join(issues)}",
            )

        payload = {
            "author":          f"urn:li:person:{request.platform_user_id}",
            "lifecycleState":  "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {
                        "text": request.content,
                    },
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    LINKEDIN_UGC_URL,
                    json=payload,
                    headers={
                        "Authorization":  f"Bearer {access_token}",
                        "Content-Type":   "application/json",
                        "X-Restli-Protocol-Version": "2.0.0",
                    },
                )

                if response.status_code == 201:
                    post_id  = response.headers.get("x-restli-id", "")
                    post_url = f"https://www.linkedin.com/feed/update/{post_id}/"
                    logger.info(
                        "LinkedIn post published: %s for piece %s",
                        post_id, request.piece_id,
                    )
                    return PublishResult(
                        success=True,
                        platform="linkedin",
                        piece_id=request.piece_id,
                        platform_post_id=post_id,
                        platform_post_url=post_url,
                    )

                # Handle error
                error_data    = response.json()
                error_message = error_data.get("message", response.text)
                error_type    = classify_error(response.status_code, error_message)

                logger.error(
                    "LinkedIn publish failed: %d %s for piece %s",
                    response.status_code, error_message, request.piece_id,
                )

                return PublishResult(
                    success=False,
                    platform="linkedin",
                    piece_id=request.piece_id,
                    error_type=error_type.value,
                    error_code=response.status_code,
                    error_message=error_message,
                    retry_after=60 if response.status_code == 429 else None,
                )

        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                platform="linkedin",
                piece_id=request.piece_id,
                error_type="TRANSIENT",
                error_code=408,
                error_message="Request timed out",
                retry_after=5,
            )
        except Exception as exc:
            logger.error("LinkedIn publisher unexpected error: %s", exc)
            return PublishResult(
                success=False,
                platform="linkedin",
                piece_id=request.piece_id,
                error_type="FATAL",
                error_code=500,
                error_message=str(exc),
            )