"""The platform_ops record: one document per registry key.

No document means "derived defaults" (see derived_default), so deploying this module changes nothing for a platform
nobody has touched: an active or partial platform is live for everyone, a planned one is not started.
Writes carry the version they were based on, so two staff members cannot overwrite each other.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from app.db.mongo import platform_ops
from app.platforms.base import PlatformDefinition

STAGES = ("not_started", "in_setup", "live", "paused", "retired")
SCOPES = ("ops_only", "selected", "everyone")
HISTORY_LIMIT = 50


class VersionConflict(Exception):
    """The record changed since the caller read it."""


def derived_default(definition: PlatformDefinition) -> dict[str, Any]:
    """The record a platform behaves from when Ops has never touched it."""
    live = definition.status in ("active", "partial")
    return {
        "platform_key": definition.key,
        "ops_stage": "live" if live else "not_started",
        "rollout": {"scope": "everyone" if live else "ops_only", "workspace_ids": []},
        "auth_test": None,
        "facts_verified": None,
        "paused": None,
        "retired": None,
        "stage_history": [],
        "updated_at": None,
        "updated_by": None,
        "version": 0,
        "derived": True,
    }


def _public(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["derived"] = False
    return out


async def get_ops(definition: PlatformDefinition) -> dict[str, Any]:
    doc = await platform_ops.find_one({"platform_key": definition.key})
    return _public(doc) if doc else derived_default(definition)


async def get_ops_many(definitions: list[PlatformDefinition]) -> dict[str, dict[str, Any]]:
    docs = {d["platform_key"]: d for d in await platform_ops.find({}).to_list(length=500)}
    return {
        definition.key: (_public(docs[definition.key]) if definition.key in docs else derived_default(definition))
        for definition in definitions
    }


async def save_ops(
    definition: PlatformDefinition,
    changes: dict[str, Any],
    *,
    expected_version: int,
    actor_id: str,
    history_entry: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Apply `changes` on top of the current record, only if it is still at `expected_version`.
    The first write for a platform starts from the derived defaults at version 0."""
    now = datetime.now(timezone.utc)
    current = await platform_ops.find_one({"platform_key": definition.key})
    base = _public(current) if current else derived_default(definition)
    if base["version"] != expected_version:
        raise VersionConflict()

    history = list(base.get("stage_history") or [])
    if history_entry:
        history.append({**history_entry, "at": now, "by": actor_id})
    history = history[-HISTORY_LIMIT:]

    document = {
        **{k: deepcopy(v) for k, v in base.items() if k not in ("derived",)},
        **changes,
        "stage_history": history,
        "updated_at": now,
        "updated_by": actor_id,
        "version": expected_version + 1,
    }
    document.pop("derived", None)

    if current is None:
        try:
            await platform_ops.insert_one(document)
        except DuplicateKeyError:
            raise VersionConflict()
    else:
        result = await platform_ops.replace_one(
            {"platform_key": definition.key, "version": expected_version}, document
        )
        if not result.matched_count:
            raise VersionConflict()
    return _public(document)
