"""What a workspace may use of the video pipeline each month, and what it has used: the minutes of finished video its renders make
and the space its videos take. The numbers are launch values for the closed test group, kept in one place so they can be tuned.

Render minutes count the length of every render that is waiting, running or done this calendar month (a failed or cancelled one
costs nothing). Storage counts the videos in the library."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.db.mongo import get_db, media_assets

#: Per plan: render minutes a month and storage in megabytes. An unknown plan gets the largest, like the other plan rules.
TIER_LIMITS: dict[str, dict[str, int]] = {
    "single": {"render_minutes": 120, "storage_mb": 2048},
    "duo": {"render_minutes": 300, "storage_mb": 5120},
    "large": {"render_minutes": 1200, "storage_mb": 20480},
}
UNKNOWN_LIMITS = TIER_LIMITS["large"]
#: A meter turns amber (and Odette flags it) from this share of the limit.
WARN_AT = 0.8


def month_start(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def limits_for(workspace: Optional[dict]) -> dict[str, int]:
    return dict(TIER_LIMITS.get(str((workspace or {}).get("tier") or ""), UNKNOWN_LIMITS))


async def render_seconds_used(workspace_id: str, since: Optional[datetime] = None) -> float:
    rows = await get_db()["video_jobs"].aggregate([
        {"$match": {"workspace_id": workspace_id, "created_at": {"$gte": since or month_start()}, "status": {"$in": ["queued", "claimed", "done"]},
                    "kind": {"$in": [None, "render"]}}},
        {"$group": {"_id": None, "seconds": {"$sum": "$duration_s"}}},
    ]).to_list(length=1)
    return float(rows[0]["seconds"]) if rows else 0.0


async def storage_bytes_used(workspace_id: str) -> int:
    rows = await media_assets.aggregate([
        {"$match": {"workspace_id": workspace_id, "kind": "video"}},
        {"$group": {"_id": None, "bytes": {"$sum": {"$ifNull": ["$size_bytes", 0]}}}},
    ]).to_list(length=1)
    return int(rows[0]["bytes"]) if rows else 0


def _meter(used: float, limit: float) -> dict[str, Any]:
    share = (used / limit) if limit else 0.0
    return {"used": round(used, 2), "limit": limit, "share": round(share, 3), "state": "over" if share >= 1 else "near" if share >= WARN_AT else "ok"}


async def usage(workspace_id: str) -> dict[str, Any]:
    """The two meters a member sees: render minutes this month and storage, from the workspace's plan limits."""
    from app.shared import entitlements

    ent = await entitlements.entitlements_for(workspace_id)
    minutes = await entitlements.count_usage(ent, "render_minutes_month")
    stored_mb = await entitlements.count_usage(ent, "storage_mb")
    return {
        "month": month_start().strftime("%Y-%m"),
        "render_minutes": _meter(minutes, ent.limit("render_minutes_month") or 0),
        "storage_mb": _meter(stored_mb, ent.limit("storage_mb") or 0),
    }


class OverLimit(Exception):
    """The workspace has used its share. `message` is plain words for the member."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def assert_render_room(workspace_id: str, seconds: float) -> None:
    """Raises OverLimit when this render would take the workspace (and every workspace sharing its plan) past its render minutes
    for the month."""
    from app.shared import entitlements

    ent = await entitlements.entitlements_for(workspace_id)
    minutes = ent.limit("render_minutes_month")
    if minutes is None:
        return
    used = await entitlements.count_usage(ent, "render_minutes_month") * 60
    if used + max(0.0, seconds) > minutes * 60:
        if ent.enforced:
            raise OverLimit(entitlements.limit_message(ent, "render_minutes_month", used / 60, max(0.0, seconds) / 60))
        left = max(0, int((minutes * 60 - used) // 60))
        raise OverLimit(
            f"This would use more than your {minutes} render minutes for this month. You have about {left} minutes left. "
            "Your minutes start again on the 1st."
        )


async def assert_storage_room(workspace_id: str, adding_bytes: int) -> None:
    """Raises a plain-words 413 (402 once plans are on) when this upload would not fit the workspace's storage. While plans are off
    only videos count, against the video space as before; once they are on, every stored file counts against the plan's storage."""
    from fastapi import HTTPException

    from app.shared import entitlements

    ent = await entitlements.entitlements_for(workspace_id)
    room_mb = ent.limit("storage_mb")
    if room_mb is None:
        return
    used_mb = await entitlements.count_usage(ent, "storage_mb")
    adding_mb = max(0, adding_bytes) / 1_000_000.0
    if used_mb + adding_mb <= room_mb:
        return
    if ent.enforced:
        raise HTTPException(status_code=402, detail=entitlements.limit_detail(ent, "storage_mb", used_mb, adding_mb))
    raise HTTPException(
        status_code=413,
        detail=f"Your videos take {used_mb:.0f} MB of the {room_mb} MB your plan includes, so this one does not fit. Remove videos you no longer need first.",
    )
