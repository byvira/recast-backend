"""LinkedIn's newer Posts API, used when a post needs what the older share API cannot do: several pictures (2 to 20), a description for
each picture, or a link card. A post with one plain picture, one video or only text keeps using the older path.

Built from LinkedIn's documented Images and Posts APIs. Not yet confirmed against a live account, which is why it is only used
when the older path cannot carry what the member asked for."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from app.pipelines.publish.base import PublishRequest, PublishResult
from app.pipelines.publish.supervisor.classifier import classify_error

logger = logging.getLogger(__name__)

LINKEDIN_REST = "https://api.linkedin.com/rest"
#: A month the Posts API accepts (versions are supported for about a year). Change it here when LinkedIn retires it.
LINKEDIN_VERSION = "202601"
MAX_PICTURES = 20
_RESERVED = re.compile(r"([|{}@\[\]()<>\\*_~])")


def escape_commentary(text: str) -> str:
    """The post's text in LinkedIn's "little text" format: characters that format text or make mentions are written with a backslash so
    the post reads exactly as typed. Hashtags are left alone, so they stay hashtags."""
    return _RESERVED.sub(r"\\\1", text)


def needs_posts_api(request: PublishRequest) -> bool:
    """Several pictures, a description on a picture, or a link card."""
    pictures = [m for m in request.media if m.kind.value == "image"]
    options = request.options or {}
    if any(m.kind.value == "video" for m in request.media):
        # a video keeps the older path unless it carries a chosen cover, which only the Videos API can take
        return bool(options.get("cover_media_id"))
    return len(pictures) >= 2 or bool(options.get("link_card_url")) or any(getattr(p, "alt_text", None) for p in pictures)


def build_content(image_urns: list[tuple[str, Optional[str]]], article: Optional[dict]) -> Optional[dict]:
    """The `content` part of the post: one picture, several pictures, or a link card. None for a text post."""
    if len(image_urns) >= 2:
        return {"multiImage": {"images": [_picture(urn, alt) for urn, alt in image_urns[:MAX_PICTURES]]}}
    if len(image_urns) == 1:
        return {"media": _picture(*image_urns[0])}
    return {"article": article} if article else None


def _picture(urn: str, alt: Optional[str]) -> dict:
    picture: dict[str, Any] = {"id": urn}
    if alt:
        picture["altText"] = alt[:4000]
    return picture


def build_post(author: str, commentary: str, visibility: str, content: Optional[dict]) -> dict:
    post: dict[str, Any] = {
        "author": author,
        "commentary": escape_commentary(commentary),
        "visibility": visibility or "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if content:
        post["content"] = content
    return post


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Linkedin-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


async def upload_image(client: httpx.AsyncClient, access_token: str, owner: str, url: str) -> str:
    """Uploads one picture with the Images API and returns its URN. Raises on any failure."""
    init = await client.post(
        f"{LINKEDIN_REST}/images", params={"action": "initializeUpload"},
        json={"initializeUploadRequest": {"owner": owner}}, headers=_headers(access_token),
    )
    init.raise_for_status()
    value = init.json()["value"]
    source = await client.get(url)
    source.raise_for_status()
    put = await client.put(value["uploadUrl"], content=source.content, headers={"Authorization": f"Bearer {access_token}"})
    put.raise_for_status()
    return value["image"]


async def upload_video(client: httpx.AsyncClient, access_token: str, owner: str, url: str, cover: Optional[bytes]) -> str:
    """Uploads one video with the Videos API (in the parts LinkedIn asks for) and returns its URN. The cover, when given, goes to the
    thumbnail address LinkedIn hands back. Raises on any failure."""
    source = await client.get(url, timeout=300.0)
    source.raise_for_status()
    data = source.content
    init = await client.post(
        f"{LINKEDIN_REST}/videos", params={"action": "initializeUpload"},
        json={"initializeUploadRequest": {"owner": owner, "fileSizeBytes": len(data), "uploadCaptions": False,
                                          "uploadThumbnail": cover is not None}},
        headers=_headers(access_token),
    )
    init.raise_for_status()
    value = init.json()["value"]
    part_ids: list[str] = []
    for part in value.get("uploadInstructions", []):
        chunk = data[int(part["firstByte"]): int(part["lastByte"]) + 1]
        put = await client.put(part["uploadUrl"], content=chunk, headers={"Content-Type": "application/octet-stream"}, timeout=300.0)
        put.raise_for_status()
        part_ids.append(put.headers.get("etag", "").strip('"'))
    if cover is not None and value.get("thumbnailUploadUrl"):
        thumb = await client.put(value["thumbnailUploadUrl"], content=cover, headers={"Content-Type": "application/octet-stream"})
        thumb.raise_for_status()
    finalize = await client.post(
        f"{LINKEDIN_REST}/videos", params={"action": "finalizeUpload"},
        json={"finalizeUploadRequest": {"video": value["video"], "uploadToken": value.get("uploadToken", ""), "uploadedPartIds": part_ids}},
        headers=_headers(access_token),
    )
    finalize.raise_for_status()
    return value["video"]


async def _cover_bytes(client: httpx.AsyncClient, request: PublishRequest) -> Optional[bytes]:
    """The chosen cover picture from this workspace's library, or None when it is missing or cannot be read."""
    media_id = (request.options or {}).get("cover_media_id")
    if not media_id:
        return None
    from app.db.mongo import media_assets

    cover = await media_assets.find_one({"id": media_id, "workspace_id": request.workspace_id, "kind": "image"})
    if not cover or not str(cover.get("url", "")).startswith("https://"):
        return None
    try:
        picture = await client.get(cover["url"], timeout=30.0)
        picture.raise_for_status()
        return picture.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the cover picture for piece %s: %s", request.piece_id, exc)
        return None


def author_urn(request: PublishRequest) -> str:
    """Who a LinkedIn post is published as: the Company Page the member chose, or else the connected profile."""
    return request.author_urn or f"urn:li:person:{request.platform_user_id}"


def _result(request: PublishRequest, response: httpx.Response, dropped: Optional[str]) -> PublishResult:
    if response.status_code == 201:
        post_id = response.headers.get("x-restli-id", "")
        return PublishResult(
            success=True, platform="linkedin", piece_id=request.piece_id, platform_post_id=post_id,
            platform_post_url=f"https://www.linkedin.com/feed/update/{post_id}/", media_dropped_reason=dropped,
        )
    try:
        message = response.json().get("message", response.text)
    except Exception:  # noqa: BLE001
        message = response.text
    logger.error("LinkedIn publish failed: %d %s for piece %s", response.status_code, message, request.piece_id)
    return PublishResult(
        success=False, platform="linkedin", piece_id=request.piece_id,
        error_type=classify_error(response.status_code, message).value, error_code=response.status_code,
        error_message=message, retry_after=60 if response.status_code == 429 else None,
    )


async def publish_with_posts_api(request: PublishRequest, access_token: str) -> PublishResult:
    """Publishes a post that has several pictures, picture descriptions or a link card."""
    owner = author_urn(request)
    options = request.options or {}
    pictures = [m for m in request.media if m.kind.value == "image"][:MAX_PICTURES]
    videos = [m for m in request.media if m.kind.value == "video"]
    dropped: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if videos:
                cover = await _cover_bytes(client, request)
                if cover is None:
                    dropped = "Your video is up, but the cover picture couldn't be used, so LinkedIn chose a frame of the video."
                video_urn = await upload_video(client, access_token, owner, videos[0].url, cover)
                post = build_post(owner, request.content, options.get("visibility") or "PUBLIC", {"media": {"id": video_urn}})
                response = await client.post(f"{LINKEDIN_REST}/posts", json=post, headers=_headers(access_token))
                return _result(request, response, dropped)
            image_urns: list[tuple[str, Optional[str]]] = []
            for picture in pictures:
                try:
                    image_urns.append((await upload_image(client, access_token, owner, picture.url), getattr(picture, "alt_text", None)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LinkedIn picture upload failed for piece %s: %s", request.piece_id, exc)
                    dropped = "A picture couldn't be uploaded to LinkedIn, so it went out without it."

            article = None
            link = options.get("link_card_url")
            if link and not image_urns:
                try:
                    from app.pipelines.publish.link_preview import fetch_preview

                    preview = await fetch_preview(link)
                    article = {"source": preview.url, "title": preview.title or preview.url, "description": preview.description[:4086]}
                    if preview.image_url:
                        try:
                            article["thumbnail"] = await upload_image(client, access_token, owner, preview.image_url)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("LinkedIn link card picture failed for %s: %s", link, exc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LinkedIn link card failed for piece %s: %s", request.piece_id, exc)
                    dropped = dropped or "The link card couldn't be built, so the post went out without it."

            post = build_post(owner, request.content, options.get("visibility") or "PUBLIC", build_content(image_urns, article))
            response = await client.post(f"{LINKEDIN_REST}/posts", json=post, headers=_headers(access_token))
            return _result(request, response, dropped)
    except httpx.TimeoutException:
        return PublishResult(
            success=False, platform="linkedin", piece_id=request.piece_id, error_type="TRANSIENT", error_code=408,
            error_message="LinkedIn request timed out.", retry_after=5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("LinkedIn publish error for piece %s: %s", request.piece_id, exc)
        return PublishResult(success=False, platform="linkedin", piece_id=request.piece_id, error_type="FATAL", error_code=500, error_message=str(exc))
