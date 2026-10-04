"""Holding and releasing scheduled posts when a platform is paused or retired.

A held post keeps its status and its scheduled time. It only gains a `hold` marker that the scheduled worker's
claim query skips, so nothing is cancelled and nothing is lost. Releasing a hold puts the post back in the queue only
if it is still allowed out (approved, scheduling checks pass) and its time has not passed; a post whose time went by
while the platform was away goes back to waiting for the member to pick a new time, never straight out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.db.mongo import content_pieces
from app.pipelines.publish.spine import check_gate, schedule_blocker, to_utc_datetime
from app.platforms.base import PlatformDefinition
from app.shared.activity import record_system

logger = logging.getLogger(__name__)

HOLD_REASONS = ("platform_paused", "platform_retired")
# A post held because Ops disconnected that workspace's account. Released when the member reconnects, never by a resume.
CONNECTION_HOLD = "ops_disconnected"


def _belongs_to(definition: PlatformDefinition) -> dict:
    """Pieces that publish to this platform: by publish target, or by the platform text on rows with no target."""
    names = [v for v in {definition.key, definition.label, definition.text_enum_value, definition.thread_enum_value} if v]
    return {"$or": [
        {"publish_target": definition.key},
        {"publish_target": {"$in": [None, ""]}, "platform": {"$in": names}},
    ]}


def default_pause_message(label: str, count: int) -> str:
    plural = "s are" if count != 1 else " is"
    return (
        f"{label} is paused by Recast for now. Your {count} scheduled post{plural} on hold. "
        "Nothing was cancelled. They will go out after it is back, once approved."
    )


async def _tell_workspaces(
    definition: PlatformDefinition, counts: dict[str, int], title: str, message_for, status: str = "warning",
) -> None:
    """One Activity Log row in each affected workspace, in plain words."""
    stamp = uuid4().hex
    for workspace_id, count in counts.items():
        try:
            await record_system(
                workspace_id=workspace_id,
                key=f"platformhold:{definition.key}:{workspace_id}:{stamp}",
                actor_name="Recast",
                category="post_published",
                title=title,
                description=message_for(count),
                status=status,
                channel=definition.key,
                href="/dashboard/calendar",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("hold notice for %s in %s was not recorded: %s", definition.key, workspace_id, exc)


async def hold_scheduled_posts(
    definition: PlatformDefinition, *, reason: str, member_message: Optional[str] = None,
) -> dict[str, int]:
    """Mark every queued post for this platform as held, and tell each affected workspace. Posts already held for
    pause are re-marked when the platform is retired. Returns the number of posts held per workspace."""
    now = datetime.now(timezone.utc)
    marker = {"reason": reason, "platform_key": definition.key, "held_at": now}
    # Queued posts that are not held yet, and posts held for the other reason (a paused platform that is then retired).
    newly = {**_belongs_to(definition), "deleted": {"$ne": True}, "publish_status": "queued", "hold": {"$exists": False}}
    upgrade = {**_belongs_to(definition), "deleted": {"$ne": True}, "hold.reason": {"$in": [r for r in HOLD_REASONS if r != reason]}}

    note = (
        "On hold while this platform is paused." if reason == "platform_paused"
        else "Can't publish: this platform is no longer available. You can still copy the post."
    )
    await content_pieces.update_many(newly, {"$set": {"hold": marker, "schedule_note": note, "updated_at": now}})
    await content_pieces.update_many(upgrade, {"$set": {"hold": marker, "schedule_note": note, "updated_at": now}})

    held = await content_pieces.find(
        {"hold.platform_key": definition.key, "hold.held_at": now}, {"workspace_id": 1},
    ).to_list(length=20000)
    counts: dict[str, int] = {}
    for piece in held:
        counts[piece.get("workspace_id", "")] = counts.get(piece.get("workspace_id", ""), 0) + 1
    counts.pop("", None)

    if reason == "platform_paused":
        await _tell_workspaces(
            definition, counts, f"{definition.label} is paused",
            lambda n: member_message or default_pause_message(definition.label, n),
        )
    else:
        await _tell_workspaces(
            definition, counts, f"{definition.label} is no longer available",
            lambda n: member_message or (
                f"{definition.label} has been retired. Your {n} scheduled post{'s' if n != 1 else ''} "
                "can't be published. Nothing was deleted, and you can still copy the content."
            ),
        )
    return counts


async def _release(
    definition: PlatformDefinition, extra: dict, *, back_title: str, back_text: str,
) -> dict[str, int]:
    """Let held posts matching `extra` go back to the queue where they may. Returns counts: released,
    still_held and to_reschedule."""
    now = datetime.now(timezone.utc)
    pieces = await content_pieces.find({**_belongs_to(definition), **extra}).to_list(length=20000)

    released = still_held = to_reschedule = 0
    per_workspace: dict[str, dict[str, int]] = {}

    def bump(workspace_id: str, kind: str) -> None:
        per_workspace.setdefault(workspace_id, {"released": 0, "still_held": 0, "to_reschedule": 0})[kind] += 1

    for piece in pieces:
        workspace_id = piece.get("workspace_id", "")
        flt = {"piece_id": piece["piece_id"]}

        gate = check_gate(piece, honour_recorded_override=True)
        blocker = await schedule_blocker(piece, workspace_id) if workspace_id else None
        if gate or blocker:
            reason = gate.message if gate else blocker[1]
            await content_pieces.update_one(flt, {"$set": {"schedule_note": f"Still on hold. {reason}", "updated_at": now}})
            still_held += 1
            bump(workspace_id, "still_held")
            continue

        when = to_utc_datetime(piece.get("publish_scheduled_at"))
        if when is None or when <= now:
            await content_pieces.update_one(flt, {
                "$set": {
                    "publish_status": "pending",
                    "publish_scheduled_at": "",
                    "schedule_note": f"Its planned time passed while {definition.label} was away. Pick a new time.",
                    "updated_at": now,
                },
                "$unset": {"hold": ""},
            })
            to_reschedule += 1
            bump(workspace_id, "to_reschedule")
            continue

        await content_pieces.update_one(flt, {"$unset": {"hold": "", "schedule_note": ""}, "$set": {"updated_at": now}})
        released += 1
        bump(workspace_id, "released")

    for workspace_id, c in per_workspace.items():
        if not workspace_id:
            continue
        bits = []
        if c["released"]:
            bits.append(f"{c['released']} scheduled post{'s' if c['released'] != 1 else ''} will go out as planned")
        if c["to_reschedule"]:
            bits.append(f"{c['to_reschedule']} need{'s' if c['to_reschedule'] == 1 else ''} a new time")
        if c["still_held"]:
            bits.append(f"{c['still_held']} stay{'s' if c['still_held'] == 1 else ''} on hold until the post is approved or reconnected")
        await _tell_workspaces(
            definition, {workspace_id: 1}, back_title,
            lambda _n, text="; ".join(bits): f"{back_text} {text.capitalize()}.",
            status="success",
        )
    return {"released": released, "still_held": still_held, "to_reschedule": to_reschedule}


async def release_held_posts(definition: PlatformDefinition) -> dict[str, int]:
    """Release every post held because the platform was paused or retired."""
    return await _release(
        definition, {"hold.reason": {"$in": list(HOLD_REASONS)}},
        back_title=f"{definition.label} is back", back_text=f"{definition.label} is available again.",
    )


async def hold_workspace_posts(definition: PlatformDefinition, workspace_id: str) -> int:
    """Hold one workspace's queued posts for this platform because its connection was disconnected by Ops."""
    now = datetime.now(timezone.utc)
    result = await content_pieces.update_many(
        {**_belongs_to(definition), "workspace_id": workspace_id, "deleted": {"$ne": True},
         "publish_status": "queued", "hold": {"$exists": False}},
        {"$set": {
            "hold": {"reason": CONNECTION_HOLD, "platform_key": definition.key, "held_at": now},
            "schedule_note": f"On hold until {definition.label} is reconnected.",
            "updated_at": now,
        }},
    )
    return result.modified_count


async def release_workspace_posts(definition: PlatformDefinition, workspace_id: str) -> dict[str, int]:
    """Release the posts held for a disconnected account, once the member has reconnected it."""
    return await _release(
        definition, {"workspace_id": workspace_id, "hold.reason": CONNECTION_HOLD},
        back_title=f"{definition.label} is reconnected", back_text=f"{definition.label} is connected again.",
    )


async def count_impact(definition: PlatformDefinition) -> dict[str, int]:
    """What a pause or retire would touch: scheduled posts, held posts and stored result rows."""
    from app.db.mongo import post_metrics, workspace_connections

    base = _belongs_to(definition)
    return {
        "connected_workspaces": len(await workspace_connections.distinct("workspace_id", {"platform": definition.key, "is_active": True})),
        "scheduled_posts": await content_pieces.count_documents({**base, "publish_status": "queued", "hold": {"$exists": False}, "deleted": {"$ne": True}}),
        "held_posts": await content_pieces.count_documents({**base, "hold.reason": {"$in": HOLD_REASONS}}),
        "analytics_rows": await post_metrics.count_documents({"platform": definition.key}),
    }
