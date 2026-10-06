"""The steps a send needs, shared by Publish Now (`api/v1/publish.py`, answers a person who is watching) and the scheduled worker
(`workers/scheduled_posts.py`, saves state and tries again later). They wait for the platform differently, so each keeps its own flow;
everything that must be the same lives here, so the two can no longer drift apart:

* finding the publisher and sign-in for a platform, and saying why one cannot be used,
* building the request from a post (text, media, settings, YouTube details),
* what is saved when a post goes out, and the event, health mark and note that go with it.

Reconnect emails are not sent from either path: the connection health monitor (`health.py`) emails the owner once per outage after
automatic recovery fails twice, so a failing post can never email on every attempt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app.db.mongo import content_pieces
from app.models.media import MediaAsset
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.health import mark_healthy
from app.pipelines.publish.registry import adapter_for, get_publisher
from app.pipelines.publish.spine import extra_media_note, media_for_publish, planned_media_note
from app.pipelines.publish.token_store import get_token

logger = logging.getLogger(__name__)


@dataclass
class Resolved:
    publisher: Any
    token: dict
    #: None when the platform can be used, otherwise "not_connected" or "unsupported".
    problem: Optional[str]


async def resolve_publisher(platform: str, workspace_id: str) -> Resolved:
    """The publisher and sign-in for a platform in a workspace. A webhook or manual-handoff platform with saved settings resolves to the
    thin adapter, which needs no sign-in; every real publisher needs one."""
    token = await get_token(workspace_id, platform)
    try:
        publisher = get_publisher(platform)
    except ValueError:
        publisher = await adapter_for(platform, workspace_id)
    if publisher is not None and not getattr(publisher, "uses_oauth_token", True):
        return Resolved(publisher, {}, None)
    if not token:
        return Resolved(publisher, {}, "not_connected")
    if publisher is None:
        return Resolved(None, token, "unsupported")
    return Resolved(publisher, token, None)


async def build_request(
    piece: dict, workspace_id: str, user_id: str, platform: str, token: dict, *, content: Optional[str] = None,
    youtube_metadata: Optional[dict] = None,
) -> PublishRequest:
    """What is sent for a post: its text (or a repaired version), the media that platform takes in the order chosen, the post's settings
    for the platform and, for YouTube, the details the member reviewed."""
    request = PublishRequest(
        piece_id=piece["piece_id"],
        workspace_id=workspace_id,
        user_id=user_id,
        brand_id=piece["brand_id"],
        platform=platform,
        content=content if content is not None else piece["content"],
        media=[MediaAsset(**m) for m in await media_for_publish(piece, workspace_id, platform)],
        youtube_metadata=youtube_metadata,
        options=piece.get("publish_options") or {},
    )
    request.platform_user_id = (token or {}).get("platform_user_id", "")
    return request


def dropped_note(result, piece: dict, platform: str) -> Optional[str]:
    """What was left out of a post that went out, in plain words: the platform's own note, more pictures than it takes, or a planned picture
    that could not be made."""
    return result.media_dropped_reason or extra_media_note(piece, platform) or planned_media_note(piece) or None


async def mark_published(
    piece: dict, workspace_id: str, platform: str, result, content: str, *, increment_attempts: bool = False,
) -> None:
    """Saves that the post is out: its link, what was left out, the real go-live time (results are measured from it), and what exactly
    went out and which version of the post that was, so a later edit never rewrites history."""
    now = datetime.now(timezone.utc)
    updates: dict = {
        "publish_status": "published",
        "updated_at": now,
        "published_at": now,
        "media_dropped_reason": dropped_note(result, piece, platform),
        "published_content": content,
        "published_version": piece.get("version_count"),
    }
    if result.platform_post_id:
        updates["platform_post_id"] = result.platform_post_id
    if result.platform_post_url:
        updates["platform_post_url"] = result.platform_post_url
    update: dict = {"$set": updates}
    if increment_attempts:
        update["$inc"] = {"publish_attempts": 1}
    await content_pieces.update_one({"piece_id": piece["piece_id"], "workspace_id": workspace_id}, update)


async def after_published(
    piece: dict, workspace_id: str, platform: str, result, *, user_id: str, role: str = "", via: Optional[str] = None,
) -> None:
    """The event the Activity Log and analytics read, and the health mark that clears a sign-in problem. Never raises: a post that is
    already out must not be reported as failed because a note could not be written."""
    try:
        from app.shared.governance_events import emit_content_published

        extra = {"via": via} if via else {}
        emit_content_published(
            workspace_id, pipeline_type=piece.get("pipeline_type", "text"), actor_user_id=user_id, actor_role=role,
            content_id=piece["piece_id"], target=platform, external_url=result.platform_post_url or "",
            media_dropped_reason=result.media_dropped_reason or "", **extra,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("emit content.published failed for %s: %s", piece.get("piece_id"), exc)
    try:
        await mark_healthy(workspace_id, platform, via="a successful publish")
    except Exception as exc:  # noqa: BLE001
        logger.error("mark_healthy failed for %s/%s: %s", workspace_id, platform, exc)
