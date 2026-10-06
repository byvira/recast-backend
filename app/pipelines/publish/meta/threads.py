"""
Threads publisher via Threads API.
Threads is owned by Meta — uses same OAuth app as Instagram/Facebook.
Text posts supported natively — no image required.
300 character limit.
"""

import logging
import httpx

from app.pipelines.publish.base import (
    PlatformPublisher,
    PublishRequest,
    PublishResult,
)
from app.pipelines.publish.validators import validate_threads
from app.pipelines.publish.meta.oauth import (
    build_auth_url,
    exchange_code,
    refresh_threads_token,
)
from app.pipelines.publish.supervisor.classifier import classify_error

logger = logging.getLogger(__name__)

THREADS_BASE = "https://graph.threads.net/v1.0"


class ThreadsPublisher(PlatformPublisher):

    def build_auth_url(self, state: str) -> str:
        return build_auth_url(state, platform="threads")

    async def exchange_token(self, code: str) -> dict:
        return await exchange_code(code, platform="threads")

    async def refresh_token(self, refresh_token: str) -> dict:
        return await refresh_threads_token(refresh_token)

    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        return validate_threads(content)

    async def publish(
        self,
        request: PublishRequest,
        access_token: str,
    ) -> PublishResult:
        """
        Publish a text post to Threads.
        Two-step: create container → publish container.
        Text-only posts work without image.
        """
        is_valid, issues = self.validate_content(request.content)
        if not is_valid:
            return PublishResult(
                success=False,
                platform="threads",
                piece_id=request.piece_id,
                error_type="FIXABLE",
                error_code=400,
                error_message=f"Content validation failed: {'; '.join(issues)}",
            )

        threads_user_id = request.platform_user_id

        # Previously hardcoded media_type=TEXT regardless of what was
        # attached — this now uses whatever attach_media() decides is
        # actually supported, and records an honest drop reason otherwise.
        media_result = self.attach_media(request)
        container_params = {"text": request.content, "access_token": access_token}
        if media_result.has_media and media_result.asset.kind.value == "image":
            container_params["media_type"] = "IMAGE"
            container_params["image_url"] = media_result.asset.url
        elif media_result.has_media and media_result.asset.kind.value == "video":
            container_params["media_type"] = "VIDEO"
            container_params["video_url"] = media_result.asset.url
        else:
            container_params["media_type"] = "TEXT"
        options = request.options or {}
        if options.get("reply_control"):
            container_params["reply_control"] = options["reply_control"]
        if options.get("topic_tag"):
            container_params["topic_tag"] = options["topic_tag"]
        if media_result.has_media and getattr(media_result.asset, "alt_text", None):
            container_params["alt_text"] = media_result.asset.alt_text[:1000]

        pictures = [m for m in request.media if m.kind.value == "image"][:20]
        is_carousel = media_result.has_media and media_result.asset.kind.value == "image" and len(pictures) >= 2

        try:
            async with httpx.AsyncClient() as client:
                if is_carousel:
                    # Several pictures: one container for each, then a carousel container that names them.
                    child_ids: list[str] = []
                    for picture in pictures:
                        child_params = {"media_type": "IMAGE", "image_url": picture.url, "is_carousel_item": "true", "access_token": access_token}
                        if getattr(picture, "alt_text", None):
                            child_params["alt_text"] = picture.alt_text[:1000]
                        child = await client.post(f"{THREADS_BASE}/{threads_user_id}/threads", params=child_params)
                        if child.status_code != 200:
                            message = child.json().get("error", {}).get("message", child.text)
                            return PublishResult(
                                success=False, platform="threads", piece_id=request.piece_id,
                                error_type=classify_error(child.status_code, message).value, error_code=child.status_code, error_message=message,
                            )
                        child_ids.append(child.json()["id"])
                    container_params.pop("image_url", None)
                    container_params.update({"media_type": "CAROUSEL", "children": ",".join(child_ids)})

                # Step 1 — Create container
                container_response = await client.post(
                    f"{THREADS_BASE}/{threads_user_id}/threads",
                    params=container_params,
                )

                if container_response.status_code != 200:
                    error_data    = container_response.json()
                    error_message = error_data.get("error", {}).get("message", container_response.text)
                    error_type    = classify_error(container_response.status_code, error_message)
                    return PublishResult(
                        success=False,
                        platform="threads",
                        piece_id=request.piece_id,
                        error_type=error_type.value,
                        error_code=container_response.status_code,
                        error_message=error_message,
                    )

                container_id = container_response.json()["id"]

                # Step 2 — Publish
                publish_response = await client.post(
                    f"{THREADS_BASE}/{threads_user_id}/threads_publish",
                    params={
                        "creation_id":  container_id,
                        "access_token": access_token,
                    },
                )

                if publish_response.status_code == 200:
                    post_id  = publish_response.json()["id"]
                    post_url = f"https://www.threads.net/@{request.platform_user_id}/post/{post_id}"
                    # The platform's own link to the post (the fallback above is built from ids and may not open it). Best effort.
                    try:
                        link_response = await client.get(
                            f"{THREADS_BASE}/{post_id}", params={"fields": "permalink", "access_token": access_token},
                        )
                        if link_response.status_code == 200 and link_response.json().get("permalink"):
                            post_url = link_response.json()["permalink"]
                    except Exception:  # noqa: BLE001
                        pass
                    logger.info(
                        "Threads post published: %s for piece %s",
                        post_id, request.piece_id,
                    )
                    return PublishResult(
                        success=True,
                        platform="threads",
                        piece_id=request.piece_id,
                        platform_post_id=post_id,
                        platform_post_url=post_url,
                        media_dropped_reason=media_result.dropped_reason,
                    )

                error_data    = publish_response.json()
                error_message = error_data.get("error", {}).get("message", publish_response.text)
                error_type    = classify_error(publish_response.status_code, error_message)

                return PublishResult(
                    success=False,
                    platform="threads",
                    piece_id=request.piece_id,
                    error_type=error_type.value,
                    error_code=publish_response.status_code,
                    error_message=error_message,
                )

        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                platform="threads",
                piece_id=request.piece_id,
                error_type="TRANSIENT",
                error_code=408,
                error_message="Request timed out",
                retry_after=5,
            )
        except Exception as exc:
            logger.error("Threads publisher error: %s", exc)
            return PublishResult(
                success=False,
                platform="threads",
                piece_id=request.piece_id,
                error_type="FATAL",
                error_code=500,
                error_message=str(exc),
            )