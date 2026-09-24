"""Activity Log read model — writes, reads and live fan-out for ``activity_entries``.

Every row belongs to one of two lanes:

* ``active``  — Remy/Odette suggestions and feedback waiting on a decision
  (accept / dismiss / snooze). Never expires while undecided.
* ``passive`` — the record of work done: people, agents and system jobs.
  Expires with the same 90-day retention as ``workspace_events``.

Rows are keyed ``f"{source_kind}:{source_id}"`` so re-projecting the same
source (a redelivered event, a status change on an insight) upserts the one
row instead of duplicating it.

Visibility mirrors the access model the sources already have:

* ``workspace`` — every active member (content, publishing, voice, members)
* ``admins``    — holders of ``view_workspace_insights`` (Odette's insights and
  flags, tier changes) — the same gate ``/api/v1/supervisor`` uses
* ``member``    — only ``member_user_id`` (Remy's signals — private to the
  member, not even admins, same as ``/api/v1/assistant``)

Nothing here raises into the caller: the Activity Log is a projection, and a
failed projection must never fail the publish / generation / agent pass that
produced it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.rbac import ROLE_PERMISSIONS
from app.db.mongo import activity_entries
from app.db.redis import get_redis

logger = logging.getLogger(__name__)

#: Matches the ``workspace_events`` TTL (app.db.mongo.create_indexes).
RETENTION = timedelta(days=90)

#: Redis pub/sub channel prefix for live rows. Pub/sub (not a stream) is
#: deliberate here: a live row missed during a reconnect is harmless because
#: the page refetches on reconnect, and Mongo is the source of truth.
LIVE_CHANNEL_PREFIX = "recast:activity:"

LANE_ACTIVE = "active"
LANE_PASSIVE = "passive"

_ADMIN_PERMISSION = "view_workspace_insights"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def can_see_admin_rows(role: str) -> bool:
    return _ADMIN_PERMISSION in ROLE_PERMISSIONS.get(role or "", set())


def visibility_filter(workspace_id: str, user_id: str, role: str) -> dict:
    """Rows this member may see — minus the ones they deleted from their own
    log (``hidden_by``; per-member, the audit trail itself is untouched)."""
    clauses: list[dict] = [
        {"visibility": "workspace"},
        {"visibility": "member", "member_user_id": user_id},
    ]
    if can_see_admin_rows(role):
        clauses.append({"visibility": "admins"})
    return {"workspace_id": workspace_id, "$or": clauses, "hidden_by": {"$ne": user_id}}


# ─────────────────────────────────────────────────────────────────────────────
# Per-member read state — stored on the row (read_by / unread_by) plus one
# "read everything before" cursor per member (app.db.mongo.inbox_state).
#
# A row is unread for a member when it isn't their own action and either
#   * they explicitly marked it unread, or
#   * they haven't opened it and it's newer than their cursor — Active items
#     (waiting on a decision) ignore the cursor: "mark all read" doesn't make
#     a pending decision go away.
# ─────────────────────────────────────────────────────────────────────────────

def unread_filter(user_id: str, read_before: Optional[datetime]) -> dict:
    fresh: dict = {"read_by": {"$ne": user_id}}
    if read_before is not None:
        fresh["$or"] = [{"lane": LANE_ACTIVE}, {"occurred_at": {"$gt": read_before}}]
    return {"$and": [
        {"actor.user_id": {"$ne": user_id}},
        {"$or": [{"unread_by": user_id}, fresh]},
    ]}


def is_unread(doc: dict, user_id: str, read_before: Optional[datetime]) -> bool:
    if (doc.get("actor") or {}).get("user_id") == user_id:
        return False
    if user_id in (doc.get("unread_by") or []):
        return True
    if user_id in (doc.get("read_by") or []):
        return False
    if doc.get("lane") == LANE_ACTIVE or read_before is None:
        return True
    occurred = doc.get("occurred_at")
    if isinstance(occurred, datetime) and occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    return occurred is not None and occurred > read_before


def is_visible_to(entry: dict, user_id: str, role: str) -> bool:
    vis = entry.get("visibility")
    if vis == "workspace":
        return True
    if vis == "member":
        return entry.get("member_user_id") == user_id
    if vis == "admins":
        return can_see_admin_rows(role)
    return False


def _search_text(entry: dict) -> str:
    actor = entry.get("actor") or {}
    parts = [
        entry.get("title", ""),
        entry.get("description", ""),
        actor.get("name", ""),
        entry.get("channel") or "",
    ]
    return " ".join(p for p in parts if p).lower()


async def upsert_entry(entry: dict) -> Optional[dict]:
    """Insert or replace one row, then push it to live subscribers.

    ``entry`` must carry ``_id``, ``workspace_id``, ``lane``, ``visibility``
    and ``occurred_at``. ``created_at`` is preserved across re-projection.
    Returns the stored row, or ``None`` on failure. Never raises.
    """
    try:
        now = _now()
        doc = dict(entry)
        doc["updated_at"] = now
        doc["search_text"] = _search_text(doc)
        if doc["lane"] == LANE_PASSIVE:
            doc["expires_at"] = (doc.get("decided_at") or doc["occurred_at"]) + RETENTION
        else:
            doc["expires_at"] = None
        entry_id = doc.pop("_id")
        # created_at is insert-only; a re-saved stored row (patch_entry)
        # carries it and would conflict with $setOnInsert.
        doc.pop("created_at", None)
        await activity_entries.update_one(
            {"_id": entry_id},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        doc["_id"] = entry_id
        await _publish_live(doc)
        return doc
    except Exception as exc:  # noqa: BLE001
        logger.error("activity upsert failed for %s: %s", entry.get("_id"), exc)
        return None


async def patch_entry(entry_id: str, fields: dict) -> Optional[dict]:
    """Update fields on an existing row (lane change, decision, snooze) and
    push the result live. No-op if the row doesn't exist. Never raises."""
    try:
        current = await activity_entries.find_one({"_id": entry_id})
        if not current:
            return None
        merged = {**current, **fields}
        return await upsert_entry(merged)
    except Exception as exc:  # noqa: BLE001
        logger.error("activity patch failed for %s: %s", entry_id, exc)
        return None


async def _publish_live(doc: dict) -> None:
    try:
        r = await get_redis()
        await r.publish(
            f"{LIVE_CHANNEL_PREFIX}{doc['workspace_id']}",
            json.dumps(doc, default=_json_default),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("activity live publish failed for %s: %s", doc.get("_id"), exc)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

#: The page's "Automations & Cron" filter covers both system actor types.
_ACTOR_FILTER = {
    "ai_agent": ["ai_agent"],
    "user": ["user"],
    "team_member": ["team_member"],
    "system_cron": ["system_cron", "webhook"],
}


def encode_cursor(doc: dict) -> str:
    return f"{doc['occurred_at'].isoformat()}|{doc['_id']}"


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    ts, _, entry_id = cursor.partition("|")
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, entry_id


def build_query(
    *,
    workspace_id: str,
    user_id: str,
    role: str,
    lane: str,
    category: Optional[str] = None,
    actor_type: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
) -> dict:
    query = visibility_filter(workspace_id, user_id, role)
    and_clauses: list[dict] = []
    query["lane"] = lane
    if lane == LANE_ACTIVE:
        # A snoozed suggestion comes back on its own once the snooze lapses.
        and_clauses.append({"$or": [
            {"snoozed_until": None},
            {"snoozed_until": {"$lte": _now()}},
        ]})
    if category:
        query["category"] = category
    if actor_type and actor_type in _ACTOR_FILTER:
        query["actor.type"] = {"$in": _ACTOR_FILTER[actor_type]}
    if status:
        query["status"] = status
    if q and q.strip():
        query["search_text"] = {"$regex": re.escape(q.strip().lower())}
    if and_clauses:
        query["$and"] = and_clauses
    return query


async def list_entries(query: dict, *, cursor: Optional[str], limit: int) -> dict:
    page_query = dict(query)
    if cursor:
        ts, entry_id = _decode_cursor(cursor)
        page_query.setdefault("$and", [])
        page_query["$and"] = list(page_query["$and"]) + [{"$or": [
            {"occurred_at": {"$lt": ts}},
            {"occurred_at": ts, "_id": {"$lt": entry_id}},
        ]}]
    docs = await activity_entries.find(page_query).sort(
        [("occurred_at", -1), ("_id", -1)]
    ).limit(limit + 1).to_list(length=limit + 1)
    has_more = len(docs) > limit
    docs = docs[:limit]
    return {
        "docs": docs,
        "next_cursor": encode_cursor(docs[-1]) if has_more and docs else None,
    }


async def count_entries(query: dict) -> int:
    return await activity_entries.count_documents(query)


async def get_entry(entry_id: str) -> Optional[dict]:
    return await activity_entries.find_one({"_id": entry_id})
