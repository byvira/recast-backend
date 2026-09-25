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
LINKEDIN_ASSETS_URL = "https://api.linkedin.com/v2/assets?action=registerUpload"

_RECIPE_BY_KIND = {
    "image": "urn:li:digitalmediaRecipe:feedshare-image",
    "video": "urn:li:digitalmediaRecipe:feedshare-video",
}


class LinkedInPublisher(PlatformPublisher):

    def build_auth_url(self, state: str) -> str:
        return build_auth_url(state)

    async def exchange_token(self, code: str) -> dict:
        return await exchange_code(code)

    async def refresh_token(self, refresh_token: str) -> dict:
        return await refresh_access_token(refresh_token)

    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        return validate_linkedin(content)

    async def _register_and_upload_asset(
        self, client: httpx.AsyncClient, person_id: str, access_token: str, asset_url: str, kind: str
    ) -> str:
        """LinkedIn's UGC API needs bytes uploaded first, unlike Meta's
        fetch-by-URL pattern — this is why LinkedIn overrides the base
        attach_media() default rather than using it directly. Three steps:
        register the upload -> PUT the actual bytes -> the returned asset
        URN goes in the post payload. Single-PUT only — LinkedIn's simpler
        recipe works for images and reasonably-sized video; very large
        video needs LinkedIn's separate multi-part upload API, not
        implemented here (a real, honest scope limit, not a silent gap).

        Returns the asset URN. Raises on any failure — the caller decides
        how to report that as a media_dropped_reason.
        """
        register_resp = await client.post(
            LINKEDIN_ASSETS_URL,
            json={
                "registerUploadRequest": {
                    "recipes": [_RECIPE_BY_KIND[kind]],
                    "owner": f"urn:li:person:{person_id}",
                    "serviceRelationships": [
                        {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
                    ],
                }
            },
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        )
        register_resp.raise_for_status()
        value = register_resp.json()["value"]
        upload_url = value["uploadMechanism"][
            "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
        ]["uploadUrl"]
        asset_urn = value["asset"]

        media_bytes_resp = await client.get(asset_url)
        media_bytes_resp.raise_for_status()

        put_resp = await client.put(
            upload_url,
            content=media_bytes_resp.content,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        put_resp.raise_for_status()

        return asset_urn

    async def publish(
        self,
        request: PublishRequest,
        access_token: str,
    ) -> PublishResult:
        """
        Publish a post to LinkedIn using the UGC Posts API — text-only, or
        with an uploaded image/video asset attached.
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

        # Previously hardcoded shareMediaCategory=NONE regardless of what
        # was attached — attach_media() only makes the "is this kind
        # supported" decision here; the actual upload mechanics below are
        # LinkedIn-specific (register -> PUT -> reference), so this doesn't
        # use the base default's URL-reference return path directly.
        media_result = self.attach_media(request)
        media_dropped_reason = media_result.dropped_reason
        share_content: dict = {
            "shareCommentary": {"text": request.content},
            "shareMediaCategory": "NONE",
        }

        if media_result.has_media:
            asset = media_result.asset
            try:
                async with httpx.AsyncClient() as upload_client:
                    asset_urn = await self._register_and_upload_asset(
                        upload_client, request.platform_user_id, access_token, asset.url, asset.kind.value
                    )
                share_content["shareMediaCategory"] = asset.kind.value.upper()
                share_content["media"] = [{"status": "READY", "media": asset_urn}]
            except Exception as exc:  # noqa: BLE001
                logger.warning("LinkedIn asset upload failed for piece %s: %s", request.piece_id, exc)
                media_dropped_reason = "LinkedIn media upload failed — published as text only"

        payload = {
            "author":          f"urn:li:person:{request.platform_user_id}",
            "lifecycleState":  "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": share_content
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
                        media_dropped_reason=media_dropped_reason,
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