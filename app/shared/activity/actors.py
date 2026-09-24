"""Actor snapshots for Activity Log rows.

A row stores the actor as they were when it happened (name, avatar, role) —
an audit trail shouldn't rewrite history when someone renames themselves.
Lookups are cached briefly in-process because one pipeline run or publish
burst resolves the same member many times in a row.
"""

from __future__ import annotations

import time
from typing import Optional

from app.db.mongo import users, workspace_members

#: Backend role → the frontend's UserRole label (types/dashboard.types.ts).
_ROLE_LABELS = {
    "owner": "Owner",
    "admin": "Admin",
    "editor": "Creator",
    "viewer": "Reviewer",
}

#: The page's actor filter splits people by role: "Admins & Owners" is
#: ``team_member``, "Creators & Writers" is ``user``.
_ADMIN_ROLES = {"owner", "admin"}

_CACHE_TTL_SECONDS = 300
_cache: dict[tuple[str, str], tuple[float, dict]] = {}

REMY = {"type": "ai_agent", "name": "Remy", "agent": "remy"}
ODETTE = {"type": "ai_agent", "name": "Odette", "agent": "odette"}


def system_actor(name: str) -> dict:
    return {"type": "system_cron", "name": name}


async def member_actor(workspace_id: str, user_id: str, role: Optional[str] = None) -> dict:
    """Snapshot of a human actor. Falls back to a neutral label rather than
    failing when the user doc is gone (deleted account, bad id)."""
    key = (workspace_id, user_id)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return dict(hit[1])

    user = await users.find_one({"id": user_id}, {"name": 1, "avatar_url": 1}) or {}
    if role is None:
        member = await workspace_members.find_one(
            {"workspace_id": workspace_id, "user_id": user_id}, {"role": 1}
        ) or {}
        role = member.get("role", "")

    actor = {
        "type": "team_member" if role in _ADMIN_ROLES else "user",
        "user_id": user_id,
        "name": user.get("name") or "Workspace member",
    }
    if user.get("avatar_url"):
        actor["avatar"] = user["avatar_url"]
    if role in _ROLE_LABELS:
        actor["role"] = _ROLE_LABELS[role]

    _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, actor)
    return dict(actor)
