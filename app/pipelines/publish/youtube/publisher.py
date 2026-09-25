"""
YouTube publisher — basics only (per product owner's explicit scope call,
2026-09-25). Full "engaging video asset" richness (chapters, captions,
end screens) is a deliberate later stage, not this build.

YouTube doesn't fit the "text post + optional attached media" shape every
other publisher uses — the video IS the post, not an attachment to one.
Recast has no video generation (see docs/DEFERRED_AND_PARTIAL_SCOPE.md,
DEF-001/002) — the video must be user-uploaded via the same
POST /api/v1/media flow every other media attach already uses. No video
attached is a real failure here, not a silent text-only fallback the way
a dropped image is for the other 5 publishers.

Title/description/tags/category are never guessed generically — always
generated for real from the piece's actual content and the brand's real
identity (app.pipelines.publish.youtube.metadata.generate_youtube_metadata),
and always user-editable: POST /api/v1/publish/youtube/prepare returns a
real draft for review, and request.youtube_metadata carries whatever the
user kept or changed into the actual publish call. If that review step was
skipped (e.g. a direct/programmatic call), this generates the same real
metadata fresh rather than falling back to a blank guess.

privacyStatus defaults to "private" — also a genuine Google policy
requirement, not just a safe choice: unverified API projects created
after July 2020 are restricted to private uploads until an audit passes
(see app/platforms/youtube.py's policy_constraints). selfDeclaredMadeForKids
defaults to False, always a real, overridable field, never silently guessed.
"""

import logging

import httpx

from app.db.mongo import brand_profiles
from app.pipelines.publish.base import (
    PlatformPublisher,
    PublishRequest,
    PublishResult,
)
from app.pipelines.publish.validators import validate_youtube
from app.pipelines.publish.youtube.metadata import YouTubeMetadata, generate_youtube_metadata
from app.pipelines.publish.google.oauth import (
    build_auth_url as google_build_auth_url,
    exchange_code as google_exchange_code,
    refresh_google_token,
)
from app.pipelines.publish.supervisor.classifier import classify_error

logger = logging.getLogger(__name__)

YOUTUBE_UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubePublisher(PlatformPublisher):

    def build_auth_url(self, state: str) -> str:
        return google_build_auth_url(state, platform="youtube")

    async def exchange_token(self, code: str) -> dict:
        return await google_exchange_code(code, platform="youtube")

    async def refresh_token(self, refresh_token: str) -> dict:
        return await refresh_google_token(refresh_token)

    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        return validate_youtube(content)

    async def _upload_video(
        self, client: httpx.AsyncClient, access_token: str, video_bytes: bytes,
        mime_type: str, metadata: YouTubeMetadata,
    ) -> str:
        """Real resumable upload: init call (gets a session URL back in the
        Location header) -> PUT the actual bytes. Returns the new video's
        id. Raises on any failure — the caller classifies and reports it."""
        init_resp = await client.post(
            YOUTUBE_UPLOAD_INIT_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            json={
                "snippet": {
                    "title": metadata.title,
                    "description": metadata.description,
                    "tags": metadata.tags,
                    "categoryId": metadata.category_id,
                },
                "status": {
                    "privacyStatus": metadata.privacy_status,
                    "selfDeclaredMadeForKids": metadata.made_for_kids,
                },
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(len(video_bytes)),
            },
        )
        init_resp.raise_for_status()
        upload_url = init_resp.headers.get("Location")
        if not upload_url:
            raise RuntimeError("YouTube upload init returned no session URL")

        upload_resp = await client.put(
            upload_url,
            content=video_bytes,
            headers={"Content-Type": mime_type},
        )
        upload_resp.raise_for_status()
        video_id = upload_resp.json().get("id")
        if not video_id:
            raise RuntimeError("YouTube upload succeeded but returned no video id")
        return video_id

    async def publish(
        self,
        request: PublishRequest,
        access_token: str,
    ) -> PublishResult:
        """Publish a video to YouTube. Requires a real video attached —
        unlike every other publisher, there is no valid text-only
        fallback here (a YouTube "video" upload with no video makes no
        sense), so a missing/unsupported video is a hard failure, not a
        media_dropped_reason on an otherwise-successful post."""
        is_valid, issues = self.validate_content(request.content)
        if not is_valid:
            return PublishResult(
                success=False,
                platform="youtube",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=f"Content validation failed: {'; '.join(issues)}",
            )

        media_result = self.attach_media(request)
        if not media_result.has_media:
            return PublishResult(
                success=False,
                platform="youtube",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=media_result.dropped_reason
                or "Attach a video to publish to YouTube.",
            )

        asset = media_result.asset

        if request.youtube_metadata:
            metadata = YouTubeMetadata(**request.youtube_metadata)
        else:
            # No reviewed draft came with this request — generate the same
            # real, brand-grounded metadata fresh rather than a blank guess.
            doc = await brand_profiles.find_one({"id": request.brand_id, "workspace_id": request.workspace_id})
            metadata = await generate_youtube_metadata(request.content, doc or {})

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                video_resp = await client.get(asset.url)
                video_resp.raise_for_status()

                video_id = await self._upload_video(
                    client, access_token, video_resp.content,
                    asset.mime_type or "video/*", metadata,
                )

                post_url = f"https://youtube.com/watch?v={video_id}"
                logger.info("YouTube video published: %s for piece %s", video_id, request.piece_id)
                return PublishResult(
                    success=True,
                    platform="youtube",
                    piece_id=request.piece_id,
                    platform_post_id=video_id,
                    platform_post_url=post_url,
                )

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            try:
                error_message = exc.response.json().get("error", {}).get("message", exc.response.text)
            except Exception:  # noqa: BLE001
                error_message = exc.response.text
            error_type = classify_error(status_code, error_message)
            logger.error(
                "YouTube publish failed: %d %s for piece %s",
                status_code, error_message, request.piece_id,
            )
            return PublishResult(
                success=False,
                platform="youtube",
                piece_id=request.piece_id,
                error_type=error_type.value,
                error_code=status_code,
                error_message=error_message,
                retry_after=60 if status_code == 429 else None,
            )
        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                platform="youtube",
                piece_id=request.piece_id,
                error_type="TRANSIENT",
                error_code=408,
                error_message="Request timed out — video uploads can be large, this may just need a retry.",
                retry_after=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("YouTube publisher unexpected error: %s", exc)
            return PublishResult(
                success=False,
                platform="youtube",
                piece_id=request.piece_id,
                error_type="FATAL",
                error_code=500,
                error_message=str(exc),
            )
