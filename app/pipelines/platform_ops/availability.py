"""platform_availability(): the single answer to "what may this workspace do with this platform right now?".

Connect accounts, the scheduling gate, the scheduled worker and the Ops screens all ask here, so the rule exists
once. The answer is the stricter of three things, applied in this order:

    1. what the code can do      (a publisher, or a webhook / manual-handoff / RSS pattern with what it needs)
    2. what Ops has set          (the stage on the platform_ops record, or the derived default)
    3. who the rollout includes  (the Ops workspace, listed workspaces, or everyone)

Values: "hidden" (not offered), "connectable" (can connect and schedule), "manual" (the member posts it themselves
or follows the listing steps), "paused" (existing connection is read-only), "retired" (history only).
A workspace that is already connected keeps its access when the rollout is narrowed; use Pause to stop everyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.db.mongo import platform_configs, users, workspace_connections
from app.pipelines.platform_ops.store import get_ops, get_ops_many
from app.pipelines.publish.platform_config_store import PLATFORM_WIDE
from app.platforms.base import PlatformDefinition, get_platform, import_all

HIDDEN, CONNECTABLE, MANUAL, PAUSED, RETIRED = "hidden", "connectable", "manual", "paused", "retired"


@dataclass(frozen=True)
class Availability:
    value: str
    reason: str


async def ops_workspace_ids() -> set[str]:
    """The workspaces that count as the Ops workspace. The OPS_WORKSPACE_IDS setting when it is filled in,
    otherwise the default workspace of every master admin."""
    listed = {part.strip() for part in settings.OPS_WORKSPACE_IDS.split(",") if part.strip()}
    if listed:
        return listed
    admins = await users.find({"is_master_admin": True}, {"default_workspace_id": 1}).to_list(length=50)
    return {a["default_workspace_id"] for a in admins if a.get("default_workspace_id")}


async def _has_settings(platform_key: str, workspace_id: str) -> bool:
    """An enabled settings row exists, platform-wide or for this workspace."""
    row = await platform_configs.find_one({
        "platform": platform_key,
        "workspace_id": {"$in": [PLATFORM_WIDE, workspace_id]},
        "enabled": {"$ne": False},
    }, {"_id": 1})
    return row is not None


async def code_capability(definition: PlatformDefinition, workspace_id: str) -> Optional[str]:
    """What the code can offer for this platform: "connectable", "manual", or None when it cannot be used at all."""
    if definition.publisher_cls:
        return CONNECTABLE
    pattern = definition.integration_pattern
    if pattern == "token_webhook":
        return CONNECTABLE if await _has_settings(definition.key, workspace_id) else None
    if pattern in ("manual_handoff", "rss_pull"):
        return MANUAL
    return None


async def _is_connected(definition: PlatformDefinition, workspace_id: str) -> bool:
    if await workspace_connections.find_one(
        {"workspace_id": workspace_id, "platform": definition.key, "is_active": True}, {"_id": 1}
    ):
        return True
    return await platform_configs.find_one(
        {"workspace_id": workspace_id, "platform": definition.key, "enabled": {"$ne": False}}, {"_id": 1}
    ) is not None


def decide(
    capable: Optional[str],
    ops: dict,
    workspace_id: str,
    in_ops_workspace: bool,
    connected: bool,
) -> Availability:
    """The pure rule: code capability first, then the Ops stage, then the rollout. `connected` is whether this
    workspace already uses the platform (it keeps access when the rollout is narrowed)."""
    if capable is None:
        return Availability(HIDDEN, "no_code")

    stage = ops["ops_stage"]
    if stage == "not_started":
        return Availability(HIDDEN, "not_started")
    if stage == "in_setup":
        return Availability(capable, "in_setup") if in_ops_workspace else Availability(HIDDEN, "in_setup")

    rollout = ops["rollout"]
    scope = rollout["scope"]
    in_rollout = (
        scope == "everyone"
        or in_ops_workspace
        or (scope == "selected" and workspace_id in rollout.get("workspace_ids", []))
    )
    included = in_rollout or connected

    if stage == "live":
        return Availability(capable, "live") if included else Availability(HIDDEN, "outside_rollout")
    if stage == "paused":
        return Availability(PAUSED, "paused") if included else Availability(HIDDEN, "outside_rollout")
    if stage == "retired":
        return Availability(RETIRED, "retired") if included else Availability(HIDDEN, "outside_rollout")
    return Availability(HIDDEN, "unknown_stage")


async def platform_availability(platform_key: str, workspace_id: str) -> Availability:
    import_all()
    definition = get_platform(platform_key)
    if definition is None:
        return Availability(HIDDEN, "unknown_platform")

    capable = await code_capability(definition, workspace_id)
    if capable is None:
        return Availability(HIDDEN, "no_code")
    ops = await get_ops(definition)
    in_ops_workspace = workspace_id in await ops_workspace_ids()
    # Only a platform that could be hidden by the rollout needs to know whether the workspace already uses it.
    connected = await _is_connected(definition, workspace_id) if ops["ops_stage"] in ("live", "paused", "retired") else False
    return decide(capable, ops, workspace_id, in_ops_workspace, connected)


async def availability_for_all(workspace_id: str, definitions: list[PlatformDefinition]) -> dict[str, Availability]:
    """platform_availability for every platform in a few queries, for screens that list them all."""
    ops_by_key = await get_ops_many(definitions)
    in_ops_workspace = workspace_id in await ops_workspace_ids()

    configs = await platform_configs.find(
        {"workspace_id": {"$in": [PLATFORM_WIDE, workspace_id]}, "enabled": {"$ne": False}}, {"platform": 1, "workspace_id": 1}
    ).to_list(length=1000)
    has_settings = {c["platform"] for c in configs}
    own_settings = {c["platform"] for c in configs if c["workspace_id"] == workspace_id}
    connected_keys = {
        c["platform"] for c in await workspace_connections.find(
            {"workspace_id": workspace_id, "is_active": True}, {"platform": 1}
        ).to_list(length=200)
    }

    result: dict[str, Availability] = {}
    for definition in definitions:
        if definition.publisher_cls:
            capable: Optional[str] = CONNECTABLE
        elif definition.integration_pattern == "token_webhook":
            capable = CONNECTABLE if definition.key in has_settings else None
        elif definition.integration_pattern in ("manual_handoff", "rss_pull"):
            capable = MANUAL
        else:
            capable = None
        connected = definition.key in connected_keys or definition.key in own_settings
        result[definition.key] = decide(capable, ops_by_key[definition.key], workspace_id, in_ops_workspace, connected)
    return result
