"""Holding a workspace's scheduled posts while its paid plan is paused, and letting them go when the plan is back.

A held post keeps its status and its scheduled time and only gains a `hold` marker, which the scheduled worker's claim query skips.
When the plan comes back, a post whose time has not passed goes back in the queue; one whose time went by while the plan was paused
waits for the member to pick a new time, never straight out. The pause covers every workspace sharing the plan.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import content_pieces
from app.pipelines.publish.spine import check_gate, schedule_blocker, to_utc_datetime
from app.shared.activity import record_system

logger = logging.getLogger(__name__)

PLAN_PAUSED = "plan_paused"


async def _scope(workspace_id: str) -> list[str]:
    from app.shared.entitlements import group_ids

    return await group_ids(workspace_id)


async def _tell(workspace_id: str, title: str, text: str, status: str) -> None:
    try:
        await record_system(
            workspace_id=workspace_id, key=f"planhold:{workspace_id}:{uuid4().hex}", actor_name="Ekavo", category="post_published",
            title=title, description=text, status=status, href="/dashboard/calendar",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("plan pause notice in %s was not recorded: %s", workspace_id, exc)


async def hold_for_pause(workspace_id: str) -> dict[str, int]:
    """Hold every queued post in the workspace (and the workspaces sharing its plan). Returns the number held per workspace."""
    now = datetime.now(timezone.utc)
    ids = await _scope(workspace_id)
    await content_pieces.update_many(
        {"workspace_id": {"$in": ids}, "deleted": {"$ne": True}, "publish_status": "queued", "hold": {"$exists": False}},
        {"$set": {"hold": {"reason": PLAN_PAUSED, "held_at": now}, "schedule_note": "On hold while your plan is paused.", "updated_at": now}},
    )
    grouped = await content_pieces.aggregate([
        {"$match": {"workspace_id": {"$in": ids}, "hold.reason": PLAN_PAUSED, "hold.held_at": now}},
        {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}},
    ]).to_list(length=None)
    counts = {row["_id"]: row["n"] for row in grouped if row.get("_id")}
    for ws_id, n in counts.items():
        await _tell(
            ws_id, "Your plan is paused",
            f"Your {n} scheduled post{'s are' if n != 1 else ' is'} on hold until the plan starts again. Nothing was cancelled.",
            "warning",
        )
    return counts


async def release_after_pause(workspace_id: str) -> dict[str, int]:
    """Let the posts held for the pause go back to the queue where they may. Returns counts: released, still_held (taken out of the
    queue because something else stops them now) and to_reschedule."""
    now = datetime.now(timezone.utc)
    ids = await _scope(workspace_id)
    released = still_held = to_reschedule = 0
    per_workspace: dict[str, dict[str, int]] = {}

    def bump(ws_id: str, kind: str) -> None:
        per_workspace.setdefault(ws_id, {"released": 0, "still_held": 0, "to_reschedule": 0})[kind] += 1

    async for piece in content_pieces.find({"workspace_id": {"$in": ids}, "hold.reason": PLAN_PAUSED}).batch_size(200):
        ws_id = piece.get("workspace_id", "")
        flt = {"piece_id": piece["piece_id"], "hold.reason": PLAN_PAUSED}
        gate = check_gate(piece, honour_recorded_override=True)
        blocker = await schedule_blocker(piece, ws_id) if ws_id else None
        if gate or blocker:
            # nothing would release this hold later, so the post leaves the queue and waits for the member, with the reason
            reason = gate.message if gate else blocker[1]
            await content_pieces.update_one(flt, {
                "$set": {"publish_status": "pending", "schedule_note": f"Not sent. {reason}", "updated_at": now},
                "$unset": {"hold": ""},
            })
            still_held += 1
            bump(ws_id, "still_held")
            continue
        when = to_utc_datetime(piece.get("publish_scheduled_at"))
        if when is None or when <= now:
            await content_pieces.update_one(flt, {
                "$set": {
                    "publish_status": "pending", "publish_scheduled_at": "",
                    "schedule_note": "Its planned time passed while your plan was paused. Pick a new time.", "updated_at": now,
                },
                "$unset": {"hold": ""},
            })
            to_reschedule += 1
            bump(ws_id, "to_reschedule")
            continue
        await content_pieces.update_one(flt, {"$unset": {"hold": "", "schedule_note": ""}, "$set": {"updated_at": now}})
        released += 1
        bump(ws_id, "released")

    for ws_id, c in per_workspace.items():
        bits = []
        if c["released"]:
            bits.append(f"{c['released']} scheduled post{'s' if c['released'] != 1 else ''} will go out as planned")
        if c["to_reschedule"]:
            bits.append(f"{c['to_reschedule']} need{'s' if c['to_reschedule'] == 1 else ''} a new time")
        if c["still_held"]:
            bits.append(f"{c['still_held']} can't go out yet and need{'s' if c['still_held'] == 1 else ''} a look")
        await _tell(ws_id, "Your plan is back", f"Your plan is running again. {'; '.join(bits).capitalize()}.", "success")
    return {"released": released, "still_held": still_held, "to_reschedule": to_reschedule}
