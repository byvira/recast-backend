"""One Activity Log row for every staff action on a platform.

Rows are written to the acting staff member's own workspace with admins-only visibility, under the category
"platform_ops", so they sit in the same log as every other recorded action. Each row's metadata carries the event
name, the platform key, the before and after values, the reason and the actor's role, which is what the platform
Activity tab reads back across workspaces. Secret values are never passed in: callers describe a secret as
"changed", never its value.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import uuid4

from app.platforms.base import PlatformDefinition
from app.shared.activity import record_system

logger = logging.getLogger(__name__)

CATEGORY = "platform_ops"

# Event name -> the plain sentence the row shows. {label} is the platform's display name.
TITLES = {
    "platform.stage_changed": "{label}: stage changed",
    "platform.rollout_changed": "{label}: rollout changed",
    "platform.config_saved": "{label}: settings saved",
    "platform.config_removed": "{label}: settings removed",
    "platform.auth_test_recorded": "{label}: live test recorded",
    "platform.facts_verified": "{label}: registry facts verified",
    "platform.paused": "{label}: paused",
    "platform.resumed": "{label}: resumed",
    "platform.retired": "{label}: retired",
    "platform.reinstated": "{label}: reinstated",
    "platform.posts_held": "{label}: scheduled posts held",
    "platform.posts_released": "{label}: held posts released",
    "connection.email_revealed": "{label}: member email revealed",
    "connection.force_disconnected": "{label}: connection force disconnected",
    "connection.reconnect_requested": "{label}: reconnect email sent",
    "connections.exported": "{label}: connections exported",
    "listing.updated": "{label}: directory listing updated",
    "platform.credentials_purged": "{label}: stored credentials purged",
    "connection.webhook_saved": "{label}: workspace webhook address saved",
    "connection.webhook_removed": "{label}: workspace webhook address removed",
}


async def record_platform_event(
    *,
    event: str,
    definition: PlatformDefinition,
    actor_user_id: str,
    actor_role: str,
    workspace_id: str,
    description: str,
    before: Any = None,
    after: Any = None,
    reason: str = "",
    subject_workspace_id: Optional[str] = None,
) -> None:
    """Never raises: a failed log write must not undo the action that was just taken."""
    try:
        await record_system(
            workspace_id=workspace_id,
            key=f"platformops:{uuid4()}",
            actor_name="Recast staff",
            actor_user_id=actor_user_id,
            category=CATEGORY,
            title=TITLES.get(event, "{label}: " + event).format(label=definition.label),
            description=description,
            channel=definition.key,
            target_id=definition.key,
            target_type="Platform",
            target_label=definition.label,
            visibility="admins",
            metadata={
                "event": event,
                "platform_key": definition.key,
                "actor_role": actor_role,
                "before": before,
                "after": after,
                "reason": reason,
                "subject_workspace_id": subject_workspace_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("platform event %s for %s was not recorded: %s", event, definition.key, exc)
