"""The context snapshot staff see beside a ticket.

Built in the background right after a ticket is filed, so a person picking it
up already knows the workspace's plan, which accounts are connected and
healthy, what failed recently, and what the member was looking at.

Only facts Recast already holds, and only the safe ones: no tokens, no OAuth
data, no full post text, nothing about other members. A ticket works fine if
this fails; staff get a "try again" button instead.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.mongo import (
    activity_entries,
    brand_profiles,
    content_pieces,
    support_ticket_context,
    support_tickets,
    workspace_connections,
    workspace_members,
    workspaces,
)

logger = logging.getLogger(__name__)

_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 3.0)
_MAX_ERRORS = 10
_ERROR_WINDOW = timedelta(hours=24)
_TEXT_LIMIT = 200

# The only fields of a linked post that are ever copied. Never the body.
_LINKED_PIECE_FIELDS = (
    "status", "platform", "approval_status", "publish_status",
    "kanban_stage", "error", "scheduled_at", "published_at", "created_at",
)


def _short(value: Any, limit: int = _TEXT_LIMIT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def _connection_health(conn: dict, now: datetime) -> str:
    if not conn.get("is_active", True):
        return "disconnected"
    expires = conn.get("expires_at")
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return "expired"
        if expires - now < timedelta(days=2):
            return "expiring_soon"
    return "healthy"


async def build_snapshot(ticket: dict) -> dict:
    now = datetime.now(timezone.utc)
    workspace_id = ticket["workspace_id"]

    workspace = await workspaces.find_one({"id": workspace_id}) or {}
    member_count = await workspace_members.count_documents({"workspace_id": workspace_id, "status": "active"})
    member_row = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": ticket["created_by"]}, {"role": 1}
    )

    connections = await workspace_connections.find({"workspace_id": workspace_id}).to_list(20)
    platforms = [
        {
            "platform": c.get("platform"),
            "username": c.get("username", ""),
            "health": _connection_health(c, now),
            "expires_at": c.get("expires_at"),
            "last_refreshed_at": c.get("last_refreshed_at"),
        }
        for c in connections
    ]

    brand_total = await brand_profiles.count_documents({"workspace_id": workspace_id})
    brand_active = await brand_profiles.count_documents({"workspace_id": workspace_id, "is_active": True})

    error_rows = (
        await activity_entries.find(
            {"workspace_id": workspace_id, "status": "failed", "occurred_at": {"$gte": now - _ERROR_WINDOW}}
        )
        .sort("occurred_at", -1)
        .to_list(_MAX_ERRORS)
    )
    errors = [
        {
            "id": str(r.get("_id")),
            "title": _short(r.get("title", "")),
            "category": r.get("category"),
            "channel": r.get("channel"),
            "occurred_at": r.get("occurred_at"),
        }
        for r in error_rows
    ]

    linked: Optional[dict] = None
    source = ticket.get("source_context") or {}
    if source.get("type") in ("post", "generation") and source.get("id"):
        piece = await content_pieces.find_one({"piece_id": source["id"], "workspace_id": workspace_id})
        if piece:
            linked = {"type": source["type"], "id": source["id"]}
            for key in _LINKED_PIECE_FIELDS:
                if key in piece and piece[key] is not None:
                    linked[key] = _short(piece[key])
    elif source.get("type") == "platform" and source.get("id"):
        linked = {"type": "platform", "id": source["id"]}

    return {
        "workspace": {
            "name": workspace.get("name", ticket.get("workspace_name", "")),
            "plan": workspace.get("tier"),
            "member_count": member_count,
            "generation_halted": bool(workspace.get("generation_halted", False)),
            "language": workspace.get("language"),
        },
        "member": {
            "name": ticket.get("created_by_name", ""),
            "role": (member_row or {}).get("role"),
        },
        "platforms": platforms,
        "brand": {"profiles": brand_total, "active": brand_active},
        "errors": errors,
        "linked_object": linked,
        "route": source.get("route"),
        "env": ticket.get("client_env") or None,
    }


async def enrich_ticket(ticket_id: str) -> dict:
    """Build and store the snapshot. Retries a few times; never raises.

    Returns the stored context document (``enrich_status`` is "done" or
    "failed")."""
    now = datetime.now(timezone.utc)
    await support_ticket_context.update_one(
        {"ticket_id": ticket_id},
        {"$set": {"enrich_status": "pending", "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    last_error: Optional[str] = None
    for attempt in range(_ATTEMPTS):
        try:
            ticket = await support_tickets.find_one({"id": ticket_id})
            if not ticket:
                break
            snapshot = await build_snapshot(ticket)
            await support_ticket_context.update_one(
                {"ticket_id": ticket_id},
                {"$set": {"snapshot": snapshot, "enrich_status": "done", "error": None,
                          "updated_at": datetime.now(timezone.utc)}},
            )
            break
        except Exception as exc:
            last_error = type(exc).__name__
            logger.warning("Support enrich attempt %d failed for ticket %s", attempt + 1, ticket_id, exc_info=True)
            if attempt < _ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_SECONDS[attempt])
    else:
        await support_ticket_context.update_one(
            {"ticket_id": ticket_id},
            {"$set": {"enrich_status": "failed", "error": last_error, "updated_at": datetime.now(timezone.utc)}},
        )
    return await support_ticket_context.find_one({"ticket_id": ticket_id}) or {}
