"""
Instagram publisher via Meta Graph API.
Requires Instagram Business or Creator account linked to Facebook Page.
Two-step process: create container → publish container.
Image is required for standard posts.
Text-only posts use the reels or carousel workaround.
"""

import json
import logging
import httpx

from app.pipelines.publish.base import (
    PlatformPublisher,
    PublishRequest,
    PublishResult,
)
from app.pipelines.publish.media_fit import jpeg_url
from app.pipelines.publish.validators import validate_instagram
from app.pipelines.publish.meta.oauth import (
    build_auth_url,
    GRAPH_BASE,
    exchange_code,
    refresh_meta_token,
)
from app.pipelines.publish.supervisor.classifier import classify_error

logger = logging.getLogger(__name__)



_READY_POLLS = 24        # about two minutes in all
_READY_POLL_SECONDS = 5


async def _wait_until_ready(client, container_id: str, access_token: str):
    """(True, None) when Instagram says the container is ready, (False, None) when it is still processing after the wait (try
    again later), (False, message) when Instagram reports it failed or expired."""
    import asyncio

    for _ in range(_READY_POLLS):
        try:
            resp = await client.get(
                f"{GRAPH_BASE}/{container_id}", params={"fields": "status_code", "access_token": access_token},
            )
            status = (resp.json() or {}).get("status_code") if resp.status_code == 200 else None
        except Exception:  # noqa: BLE001
            status = None
        if status == "FINISHED":
            return True, None
        if status in ("ERROR", "EXPIRED"):
            return False, "Instagram could not process this video. Check its size and length, then try again."
        await asyncio.sleep(_READY_POLL_SECONDS)
    return False, None


def _tag_positions(usernames: list[str]) -> list[dict]:
    """People tagged in the first picture. Instagram wants a position for each; they are placed side by side across the middle."""
    count = max(len(usernames), 1)
    return [{"username": name, "x": round((index + 1) / (count + 1), 2), "y": 0.5} for index, name in enumerate(usernames)]


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
        media_result = self.attach_media(request)

        if not media_result.has_media:
            return PublishResult(
                success=False,
                platform="instagram",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=(
                    media_result.dropped_reason
                    or "Instagram requires an image or video. Attach media before publishing."
                ),
            )

        asset = media_result.asset
        # image_url for photos; video_url + media_type=REELS for video — the
        # same container-create endpoint, a different param shape per kind.
        # Extends this publisher beyond the single-image-only it was before.
        container_params = {"caption": request.content, "access_token": access_token}
        options = request.options or {}
        if options.get("location_id"):
            container_params["location_id"] = options["location_id"]
        if options.get("collaborators"):
            container_params["collaborators"] = json.dumps(options["collaborators"])
        if asset.kind.value == "video":
            container_params["video_url"] = asset.url
            container_params["media_type"] = "REELS"
        else:
            # Instagram photo posts take JPEG; our pictures are PNG, so ask for the JPEG version of the same picture.
            container_params["image_url"] = jpeg_url(asset.url)
            if getattr(asset, "alt_text", None):
                container_params["alt_text"] = asset.alt_text[:1000]
            if options.get("user_tags"):
                container_params["user_tags"] = json.dumps(_tag_positions(options["user_tags"]))

        # Several pictures on one post go out as an Instagram carousel (2 to 10 pictures). Built from Meta's documented carousel
        # steps (a container per picture, then one carousel container naming them); not yet confirmed against a live account.
        carousel_pictures = [m for m in request.media if m.kind.value == "image"][:10]
        carousel_urls = [jpeg_url(m.url) for m in carousel_pictures]
        is_carousel = asset.kind.value == "image" and len(carousel_urls) >= 2

        try:
            # A generous timeout: Instagram fetches the picture from our host while it answers, which takes longer than the
            # few seconds the default allows (found by a live test: a plain photo timed out).
            async with httpx.AsyncClient(timeout=60.0) as client:
                if is_carousel:
                    child_ids: list[str] = []
                    for position, (url, picture) in enumerate(zip(carousel_urls, carousel_pictures)):
                        child_params = {"image_url": url, "is_carousel_item": "true", "access_token": access_token}
                        if position == 0 and options.get("user_tags"):
                            child_params["user_tags"] = json.dumps(_tag_positions(options["user_tags"]))
                        if getattr(picture, "alt_text", None):
                            child_params["alt_text"] = picture.alt_text[:1000]
                        child = await client.post(f"{GRAPH_BASE}/{ig_user_id}/media", params=child_params)
                        if child.status_code != 200:
                            error_data = child.json()
                            error_message = error_data.get("error", {}).get("message", child.text)
                            return PublishResult(
                                success=False, platform="instagram", piece_id=request.piece_id,
                                error_type=classify_error(child.status_code, error_message).value,
                                error_code=child.status_code, error_message=error_message,
                            )
                        # each picture must finish processing before the carousel names it
                        child_ready, child_problem = await _wait_until_ready(client, child.json()["id"], access_token)
                        if not child_ready:
                            return PublishResult(
                                success=False, platform="instagram", piece_id=request.piece_id,
                                error_type="TRANSIENT" if child_problem is None else "FATAL", error_code=500,
                                error_message=child_problem or "Instagram is still processing the pictures. It will be tried again shortly.",
                            )
                        child_ids.append(child.json()["id"])
                    container_params = {
                        "caption": request.content, "access_token": access_token,
                        "media_type": "CAROUSEL", "children": ",".join(child_ids),
                    }
                    if options.get("location_id"):
                        container_params["location_id"] = options["location_id"]
                    if options.get("collaborators"):
                        container_params["collaborators"] = json.dumps(options["collaborators"])

                # Step 1 — Create media container
                container_response = await client.post(
                    f"{GRAPH_BASE}/{ig_user_id}/media",
                    params=container_params,
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

                # Instagram processes every container (a picture, a carousel, a video) after it is made, and publishing before it
                # is ready fails with "Media ID is not available" (found by a live test: even a plain photo needs the wait).
                # Wait for Meta's container status to say it is ready.
                ready, wait_problem = await _wait_until_ready(client, container_id, access_token)
                if not ready:
                    return PublishResult(
                        success=False,
                        platform="instagram",
                        piece_id=request.piece_id,
                        error_type="TRANSIENT" if wait_problem is None else "FATAL",
                        error_code=500,
                        error_message=wait_problem or "Instagram is still processing the post. It will be tried again shortly.",
                    )

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
                    # The link to the post is its permalink, which Instagram gives for the media id. The id itself is not
                    # a link, so a link built from it did not open the post. Falls back to the old form if it cannot be read.
                    post_url = f"https://www.instagram.com/p/{post_id}/"
                    try:
                        link_response = await client.get(
                            f"{GRAPH_BASE}/{post_id}", params={"fields": "permalink", "access_token": access_token},
                        )
                        if link_response.status_code == 200 and link_response.json().get("permalink"):
                            post_url = link_response.json()["permalink"]
                    except Exception:  # noqa: BLE001
                        pass
                    logger.info(
                        "Instagram post published: %s for piece %s",
                        post_id, request.piece_id,
                    )
                    # The first comment is a separate step after the post is up. If it fails the post still stands, and the member
                    # is told, so it is never lost silently.
                    comment_note = None
                    if options.get("first_comment"):
                        try:
                            comment = await client.post(
                                f"{GRAPH_BASE}/{post_id}/comments",
                                params={"message": options["first_comment"], "access_token": access_token},
                            )
                            if comment.status_code != 200:
                                comment_note = "Your post is up, but the first comment couldn't be added. You can add it on Instagram."
                        except Exception:  # noqa: BLE001
                            comment_note = "Your post is up, but the first comment couldn't be added. You can add it on Instagram."
                    return PublishResult(
                        success=True,
                        platform="instagram",
                        piece_id=request.piece_id,
                        platform_post_id=post_id,
                        platform_post_url=post_url,
                        media_dropped_reason=comment_note,
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