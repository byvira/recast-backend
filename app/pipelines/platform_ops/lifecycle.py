"""Stage changes for a platform, and the guards and effects of each one.

Stages: not_started -> in_setup -> live, then paused and retired (and back from retired to in_setup).
Every change is checked against who may make it and what the platform's readiness is, written to the platform_ops
record with the version it was based on, and logged. A refused change returns the exact reasons so the screen can
show them.

Effects on scheduled posts: pausing holds them (nothing is cancelled), resuming or going live releases the ones that
are still allowed out, and retiring holds them for good with a plain note. See holds.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.db.mongo import workspace_connections
from app.pipelines.platform_ops.events import record_platform_event
from app.pipelines.platform_ops.holds import hold_scheduled_posts, release_held_posts
from app.pipelines.platform_ops.readiness import build_readiness
from app.pipelines.platform_ops.store import save_ops
from app.platforms.base import PlatformDefinition

# (from, to) -> who may make the change: "owner" is a master admin, "staff" is any platform staff member.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("not_started", "in_setup"): "owner",
    ("in_setup", "not_started"): "owner",
    ("in_setup", "live"): "owner",
    ("live", "paused"): "staff",
    ("paused", "live"): "owner",
    ("live", "retired"): "owner",
    ("paused", "retired"): "owner",
    ("retired", "in_setup"): "owner",
}


class TransitionRefused(Exception):
    def __init__(self, code: str, message: str, blockers: Optional[list[str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.blockers = blockers or []


async def _has_connections(definition: PlatformDefinition) -> bool:
    return await workspace_connections.find_one({"platform": definition.key, "is_active": True}, {"_id": 1}) is not None


async def change_stage(
    definition: PlatformDefinition,
    ops: dict[str, Any],
    config: Optional[dict[str, Any]],
    *,
    to: str,
    actor: dict[str, Any],
    workspace_id: str,
    reason: str = "",
    member_message: str = "",
    confirm_name: str = "",
) -> dict[str, Any]:
    """Move a platform to `to`. `ops` is the record as the caller read it (its version is the one checked on
    save). Raises TransitionRefused with the reasons, or VersionConflict if the record changed meanwhile."""
    current = ops["ops_stage"]
    if definition.integration_pattern == "generation_only":
        raise TransitionRefused("not_applicable", f"{definition.label} is a content shape, it has no stages.")

    needed = TRANSITIONS.get((current, to))
    if needed is None:
        raise TransitionRefused("invalid_transition", f"A platform cannot go from {current} to {to}.")
    is_owner = bool(actor.get("is_master_admin"))
    if needed == "owner" and not is_owner:
        raise TransitionRefused("owner_only", "Only the Ops owner can make this change.")

    changes: dict[str, Any] = {"ops_stage": to}
    now = datetime.now(timezone.utc)

    if (current, to) == ("in_setup", "not_started") and await _has_connections(definition):
        raise TransitionRefused("has_connections", "Workspaces are connected to this platform, so it cannot go back.")

    if (current, to) == ("in_setup", "live"):
        readiness = build_readiness(definition, ops, config)
        if not readiness["can_go_live"]:
            raise TransitionRefused("not_ready", "Some go-live checks are not passing yet.", readiness["blockers"])
        # A platform that has just gone live is available to the Ops workspace only, until the rollout is widened.
        changes["rollout"] = {"scope": "ops_only", "workspace_ids": []}

    if to == "paused":
        if not reason.strip():
            raise TransitionRefused("reason_required", "Say why the platform is being paused.")
        changes["paused"] = {"at": now, "by": actor["id"], "reason": reason, "member_message": member_message or None}

    if (current, to) == ("paused", "live"):
        code_blockers = [
            b for b in build_readiness(definition, ops, config)["blockers"] if b in ("publisher", "validator")
        ]
        if code_blockers:
            raise TransitionRefused("not_ready", "The code behind this platform is no longer complete.", code_blockers)
        changes["paused"] = None

    if to == "retired":
        if not reason.strip():
            raise TransitionRefused("reason_required", "Say why the platform is being retired.")
        if confirm_name.strip().lower() != definition.label.lower():
            raise TransitionRefused("confirm_name", f"Type {definition.label} to confirm.")
        changes["retired"] = {"at": now, "by": actor["id"], "reason": reason}
        changes["paused"] = None

    if (current, to) == ("retired", "in_setup"):
        changes["retired"] = None

    saved = await save_ops(
        definition, changes,
        expected_version=ops["version"], actor_id=actor["id"],
        history_entry={"from": current, "to": to, "reason": reason},
    )

    role = "owner" if is_owner else "staff"
    event_for = {
        "paused": "platform.paused",
        "retired": "platform.retired",
    }.get(to)
    if (current, to) == ("paused", "live"):
        event_for = "platform.resumed"
    elif (current, to) == ("retired", "in_setup"):
        event_for = "platform.reinstated"
    await record_platform_event(
        event=event_for or "platform.stage_changed", definition=definition, actor_user_id=actor["id"],
        actor_role=role, workspace_id=workspace_id,
        description=f"{definition.label} moved from {current} to {to}." + (f" Reason: {reason}" if reason else ""),
        before=current, after=to, reason=reason,
    )

    # Effects on scheduled posts, after the stage is stored so a post cannot be released into a stage that was refused.
    if to == "paused":
        counts = await hold_scheduled_posts(definition, reason="platform_paused", member_message=member_message or None)
        await record_platform_event(
            event="platform.posts_held", definition=definition, actor_user_id=actor["id"], actor_role=role,
            workspace_id=workspace_id, description=f"{sum(counts.values())} scheduled posts were held.",
            after=sum(counts.values()),
        )
    elif to == "retired":
        counts = await hold_scheduled_posts(definition, reason="platform_retired")
        await record_platform_event(
            event="platform.posts_held", definition=definition, actor_user_id=actor["id"], actor_role=role,
            workspace_id=workspace_id, description=f"{sum(counts.values())} scheduled posts were held for good.",
            after=sum(counts.values()),
        )
    elif to == "live":
        released = await release_held_posts(definition)
        if any(released.values()):
            await record_platform_event(
                event="platform.posts_released", definition=definition, actor_user_id=actor["id"], actor_role=role,
                workspace_id=workspace_id,
                description=(
                    f"{released['released']} posts released, {released['to_reschedule']} need a new time, "
                    f"{released['still_held']} still held."
                ),
                after=released,
            )
    return saved
