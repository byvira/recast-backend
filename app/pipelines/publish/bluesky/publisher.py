"""
Bluesky publisher — AT Protocol (ATP).
No OAuth — uses app passwords for authentication.
Session token obtained via com.atproto.server.createSession.
Posts via app.bsky.feed.post lexicon.
300 character limit per post.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.pipelines.publish.base import (
    PlatformPublisher,
    PublishRequest,
    PublishResult,
)
from app.pipelines.publish.validators import validate_bluesky
from app.pipelines.publish.supervisor.classifier import classify_error

logger = logging.getLogger(__name__)

ATP_BASE_URL    = "https://bsky.social/xrpc"
CREATE_SESSION  = f"{ATP_BASE_URL}/com.atproto.server.createSession"
REFRESH_SESSION = f"{ATP_BASE_URL}/com.atproto.server.refreshSession"
CREATE_POST     = f"{ATP_BASE_URL}/app.bsky.feed.post"


class BlueSkyPublisher(PlatformPublisher):

    def build_auth_url(self, state: str) -> str:
        """
        Bluesky uses app passwords — no OAuth URL needed.
        Returns empty string — frontend handles app password collection.
        """
        return ""

    async def exchange_token(self, code: str) -> dict:
        """
        For Bluesky code is formatted as "handle|app_password".
        Creates an ATP session and returns access + refresh JWTs.
        """
        try:
            handle, app_password = code.split("|", 1)
        except ValueError:
            raise ValueError(
                "Bluesky credentials must be formatted as 'handle|app_password'"
            )

        return await self._create_session(handle.strip(), app_password.strip())

    async def _create_session(self, handle: str, app_password: str) -> dict:
        """Create ATP session from handle + app password."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                CREATE_SESSION,
                json={
                    "identifier": handle,
                    "password":   app_password,
                },
            )
            response.raise_for_status()
            data = response.json()

            return {
                "access_token":     data["accessJwt"],
                "refresh_token":    data["refreshJwt"],
                "expires_at":       None,   # ATP does not return expiry — refresh proactively
                "platform_user_id": data["did"],
                "username":         data.get("handle", handle),
                "email":            "",
            }

    async def refresh_token(self, refresh_token: str) -> dict:
        """Refresh ATP session using refreshJwt."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                REFRESH_SESSION,
                headers={"Authorization": f"Bearer {refresh_token}"},
            )
            response.raise_for_status()
            data = response.json()

            return {
                "access_token": data["accessJwt"],
                "expires_at":   None,
            }

    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        return validate_bluesky(content)

    async def publish(
        self,
        request: PublishRequest,
        access_token: str,
    ) -> PublishResult:
        """
        Publish a post to Bluesky via ATP app.bsky.feed.post.
        """
        is_valid, issues = self.validate_content(request.content)
        if not is_valid:
            return PublishResult(
                success=False,
                platform="bluesky",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=f"Content validation failed: {'; '.join(issues)}",
            )

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        payload = {
            "repo":       request.platform_user_id,   # DID
            "collection": "app.bsky.feed.post",
            "record": {
                "$type":     "app.bsky.feed.post",
                "text":      request.content,
                "createdAt": now,
            },
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{ATP_BASE_URL}/com.atproto.repo.createRecord",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type":  "application/json",
                    },
                )

                if response.status_code == 200:
                    data     = response.json()
                    post_uri = data.get("uri", "")
                    post_cid = data.get("cid", "")

                    # Build web URL from AT URI
                    # at://did:plc:xxx/app.bsky.feed.post/recordkey
                    # → https://bsky.app/profile/did:plc:xxx/post/recordkey
                    post_url = ""
                    if post_uri.startswith("at://"):
                        parts = post_uri.replace("at://", "").split("/")
                        if len(parts) == 3:
                            did        = parts[0]
                            record_key = parts[2]
                            post_url   = f"https://bsky.app/profile/{did}/post/{record_key}"

                    logger.info(
                        "Bluesky post published: %s for piece %s",
                        post_uri, request.piece_id,
                    )

                    return PublishResult(
                        success=True,
                        platform="bluesky",
                        piece_id=request.piece_id,
                        platform_post_id=post_uri,
                        platform_post_url=post_url,
                    )

                error_data    = response.json()
                error_message = error_data.get("message", response.text)
                error_type    = classify_error(response.status_code, error_message)

                logger.error(
                    "Bluesky publish failed: %d %s for piece %s",
                    response.status_code, error_message, request.piece_id,
                )

                return PublishResult(
                    success=False,
                    platform="bluesky",
                    piece_id=request.piece_id,
                    error_type=error_type.value,
                    error_code=response.status_code,
                    error_message=error_message,
                )

        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                platform="bluesky",
                piece_id=request.piece_id,
                error_type="TRANSIENT",
                error_code=408,
                error_message="Request timed out",
                retry_after=5,
            )
        except Exception as exc:
            logger.error("Bluesky publisher unexpected error: %s", exc)
            return PublishResult(
                success=False,
                platform="bluesky",
                piece_id=request.piece_id,
                error_type="FATAL",
                error_code=500,
                error_message=str(exc),
            )