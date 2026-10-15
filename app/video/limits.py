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
    """The two meters a member sees: render minutes this month and storage."""
    ws = await get_db()["workspaces"].find_one({"id": workspace_id}, {"tier": 1})
    lim = limits_for(ws)
    seconds = await render_seconds_used(workspace_id)
    stored = await storage_bytes_used(workspace_id)
    return {
        "month": month_start().strftime("%Y-%m"),
        "render_minutes": _meter(seconds / 60.0, lim["render_minutes"]),
        "storage_mb": _meter(stored / 1_000_000.0, lim["storage_mb"]),
    }


class OverLimit(Exception):
    """The workspace has used its share. `message` is plain words for the member."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def assert_render_room(workspace_id: str, seconds: float) -> None:
    """Raises OverLimit when this render would take the workspace past its render minutes for the month."""
    ws = await get_db()["workspaces"].find_one({"id": workspace_id}, {"tier": 1})
    limit = limits_for(ws)["render_minutes"] * 60
    used = await render_seconds_used(workspace_id)
    if used + max(0.0, seconds) > limit:
        left = max(0, int((limit - used) // 60))
        raise OverLimit(
            f"This would use more than your {limits_for(ws)['render_minutes']} render minutes for this month. You have about {left} minutes left. "
            "Your minutes start again on the 1st."
        )
