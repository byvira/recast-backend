"""Knows when a published post is no longer on its platform.

A post's `platform_state` is "live" while the platform still shows it, "removed" once it is gone, and "unreachable" when the
connection to the platform is refused so it cannot be checked. A post is only called removed after the platform said it was
gone on two checks in a row, so one odd answer never hides a live post. A removed post keeps its history and its last
numbers, stops being checked, and has its link hidden by the screens.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.mongo import content_pieces

logger = logging.getLogger(__name__)

#: How many "gone" answers in a row make a post removed.
REMOVED_AFTER_CHECKS = 2


async def note_readable(workspace_id: str, piece_id: str) -> None:
    """The platform showed the post: it is live, and any earlier doubt is cleared."""
    if not piece_id:
        return
    await content_pieces.update_one(
        {"workspace_id": workspace_id, "piece_id": piece_id},
        {"$set": {"platform_state": "live", "platform_missing_checks": 0, "platform_checked_at": datetime.now(timezone.utc)}},
    )


async def note_unreadable(workspace_id: str, post: dict, failure: Optional[str]) -> Optional[str]:
    """Record a failed read and return the post's new state ("removed", "unreachable") when it changed, else None."""
    piece_id = post.get("piece_id") or ""
    if not piece_id or failure not in ("not_found", "auth_error"):
        return None
    now = datetime.now(timezone.utc)
    if failure == "auth_error":
        await content_pieces.update_one(
            {"workspace_id": workspace_id, "piece_id": piece_id, "platform_state": {"$ne": "removed"}},
            {"$set": {"platform_state": "unreachable", "platform_checked_at": now}},
        )
        return "unreachable"

    doc = await content_pieces.find_one_and_update(
        {"workspace_id": workspace_id, "piece_id": piece_id, "platform_state": {"$ne": "removed"}},
        {"$inc": {"platform_missing_checks": 1}, "$set": {"platform_checked_at": now}},
        return_document=True,
    )
    if not doc or int(doc.get("platform_missing_checks") or 0) < REMOVED_AFTER_CHECKS:
        return None
    await content_pieces.update_one(
        {"workspace_id": workspace_id, "piece_id": piece_id},
        {"$set": {"platform_state": "removed", "platform_removed_at": now}},
    )
    await _tell_the_member(workspace_id, doc)
    return "removed"


async def _tell_the_member(workspace_id: str, piece: dict) -> None:
    try:
        from app.shared.activity import record_system

        platform = piece.get("platform") or "the platform"
        await record_system(
            workspace_id=workspace_id, key=f"post-removed:{piece.get('piece_id')}", actor_name="Recast",
            actor_user_id=piece.get("created_by"), category="content_generated",
            title=f"A {platform} post is no longer there",
            description="It was removed on the platform. Recast keeps your copy and its history, and has stopped tracking its numbers.",
            status="success", target_id=piece.get("piece_id"), target_type="Post", href="/dashboard/drafts",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write the removed-post note for %s: %s", piece.get("piece_id"), exc)


def _post_for_fetch(piece: dict) -> dict:
    from app.pipelines.publish.spine import platform_key

    return {
        "piece_id": piece.get("piece_id") or "",
        "platform": platform_key(piece.get("platform", "")),
        "platform_post_id": piece.get("platform_post_id"),
        "platform_user_id": "",
    }


async def check_now(workspace_id: str, piece: dict) -> dict:
    """Ask the platform about one published post right now and say what it found.

    Returns {"state": "live" | "removed" | "unreachable" | "unknown", "checked_at": ISO text}. "unknown" means the platform
    could not be asked (no connection, or a platform with no reader), so nothing is changed."""
    from app.pipelines.analytics.aggregator import fetch_post_metrics_all
    from app.pipelines.publish.spine import iso_utc

    post = _post_for_fetch(piece)
    if not piece.get("platform_post_id"):
        return {"state": "unknown", "checked_at": None}
    try:
        results = await fetch_post_metrics_all(workspace_id=workspace_id, posts=[post])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Link check failed for %s: %s", piece.get("piece_id"), exc)
        return {"state": "unknown", "checked_at": None}
    doc = await content_pieces.find_one(
        {"workspace_id": workspace_id, "piece_id": piece.get("piece_id")},
        {"platform_state": 1, "platform_checked_at": 1},
    ) or {}
    state = doc.get("platform_state")
    if results and state in (None, "live"):
        state = "live"
    return {"state": state or "unknown", "checked_at": iso_utc(doc.get("platform_checked_at")) if doc.get("platform_checked_at") else None}
