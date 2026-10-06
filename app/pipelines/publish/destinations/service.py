"""Sending a Blog or Newsletter post to its destination, for both the member's own click (`api/v1/destinations.py`) and the scheduled
worker (`workers/scheduled_posts.py`). The two differ only in what they do with the outcome, so the checks, the settings read and the
call to the destination live here.
"""
from __future__ import annotations

from typing import Optional

from app.pipelines.publish import executor
from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.destinations import ghost, mailchimp, wordpress
from app.pipelines.publish.destinations.common import DestinationError
from app.pipelines.publish.spine import platform_key
from app.pipelines.publish.token_store import get_token

#: Which destinations each kind of post can go to.
DESTINATIONS = {"blog": ("wordpress", "ghost"), "newsletter": ("mailchimp",)}
LABELS = {"wordpress": "WordPress", "ghost": "Ghost", "mailchimp": "Mailchimp"}

#: A destination that answered "slow down" created nothing, so a later try is safe. Anything else might already have made the post.
SAFE_TO_RETRY_CODE = 429


def is_destination_kind(platform: str) -> bool:
    return platform_key(platform) in DESTINATIONS


def destination_of(piece: dict) -> Optional[str]:
    """The destination chosen on a Blog or Newsletter post, when it fits the kind of post."""
    kind = platform_key(piece.get("platform"))
    chosen = (piece.get("publish_options") or {}).get("destination")
    return chosen if chosen in DESTINATIONS.get(kind, ()) else None


def title_of(piece: dict) -> str:
    seo = piece.get("seo") or {}
    if (seo.get("title") or "").strip():
        return seo["title"].strip()
    for line in (piece.get("content") or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:200]
    return ""


def body_of(piece: dict) -> str:
    """The post without the heading line the editor copies in front of it, since the title is sent on its own."""
    lines = (piece.get("content") or "").splitlines()
    if lines and lines[0].startswith("# ") and lines[0][2:].strip() == (piece.get("seo") or {}).get("title", "").strip():
        return "\n".join(lines[1:]).lstrip()
    return piece.get("content") or ""


def sends_to_audience(piece: dict) -> bool:
    """True when sending this post puts it in front of a whole audience, not into a draft."""
    return destination_of(piece) == "mailchimp" and (piece.get("publish_options") or {}).get("send_mode") == "send"


def is_live(piece: dict) -> bool:
    options = piece.get("publish_options") or {}
    return sends_to_audience(piece) or options.get("post_status") == "publish"


async def blocker(piece: dict, workspace_id: str) -> Optional[tuple[int, str]]:
    """(status code, plain message) for the first reason this post cannot go to its destination, or None."""
    destination = destination_of(piece)
    if not destination:
        return 400, "Choose where to publish this post first."
    if not await get_token(workspace_id, destination):
        return 400, f"{LABELS[destination]} is not connected. Connect it first."
    if not title_of(piece):
        return 422, "Add a title before publishing."
    if destination == "mailchimp" and not (piece.get("publish_options") or {}).get("audience_id"):
        return 422, "Choose which Mailchimp audience this goes to."
    return None


async def send_piece(piece: dict, workspace_id: str, *, may_send_to_audience: bool) -> PublishResult:
    """Sends one post to its destination and returns what happened. Raises DestinationError for a problem found before anything is
    sent. A newsletter goes to its audience only when `may_send_to_audience` says the member confirmed it."""
    problem = await blocker(piece, workspace_id)
    if problem:
        raise DestinationError(problem[1])
    destination = destination_of(piece)
    options = piece.get("publish_options") or {}
    token = await get_token(workspace_id, destination)
    seo = piece.get("seo") or {}
    send = sends_to_audience(piece)
    if send and not may_send_to_audience:
        raise DestinationError("Confirm that this should be sent to the whole audience now.")
    pictures = await executor.pictures_for(piece, workspace_id)

    if destination == "mailchimp":
        return await mailchimp.publish(
            api_key=token["access_token"], piece_id=piece["piece_id"], audience_id=options["audience_id"], subject=title_of(piece),
            preview=seo.get("meta_description") or "", content=piece.get("content") or "", send=send,
        )
    if destination == "wordpress":
        return await wordpress.publish(
            site=token["platform_user_id"], username=token["username"], password=token["access_token"], piece_id=piece["piece_id"],
            title=title_of(piece), content=body_of(piece), excerpt=seo.get("meta_description") or "", slug=seo.get("slug") or "",
            status=options.get("post_status") or "draft", category_id=options.get("category_id") or "", author_id=options.get("author_id") or "",
            pictures=pictures,
        )
    return await ghost.publish(
        site=token["platform_user_id"], admin_key=token["access_token"], piece_id=piece["piece_id"], title=title_of(piece), content=body_of(piece),
        excerpt=seo.get("meta_description") or "", slug=seo.get("slug") or "", status=options.get("post_status") or "draft",
        tags=seo.get("tags") or [], pictures=pictures,
    )


async def record_success(piece: dict, workspace_id: str, result: PublishResult, *, user_id: str, role: str = "", via: Optional[str] = None) -> None:
    """Saves that the post is at its destination, with whether it is live or left as a draft there."""
    from app.db.mongo import content_pieces

    destination = destination_of(piece)
    await executor.mark_published(piece, workspace_id, destination, result, piece.get("content") or "", increment_attempts=True)
    await executor.after_published(piece, workspace_id, destination, result, user_id=user_id, role=role, via=via)
    # A draft left at the destination is still "published" here, so what the member sees must say it is a draft.
    await content_pieces.update_one(
        {"piece_id": piece["piece_id"], "workspace_id": workspace_id},
        {"$set": {"publish_destination": destination, "publish_destination_state": "live" if is_live(piece) else "draft"}},
    )
