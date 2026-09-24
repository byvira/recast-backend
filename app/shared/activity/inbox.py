"""Read / unread / delete for the Activity Log, and the Inbox built on it.

One read state per member, shared by the Activity Log rows, the Inbox and the
sidebar's unread count (see ``store.unread_filter`` for the rule):

* per row — ``read_by`` / ``unread_by`` (open, mark read, mark unread)
* per member — ``inbox_state.read_before`` ("Mark all read")
* delete — ``hidden_by``: removes the row from *that member's* log only; the
  audit trail other members see is untouched. Items still waiting on a
  decision can't be deleted — they're dismissed instead.

The **Inbox** is the subset worth interrupting someone for:

* ``lane == active``        — waiting on their decision
* ``status == failed``      — something broke (publish, connection, campaign)
* autonomous work           — done by an agent/system on their behalf
* their own pipeline runs   — finished while they may have left the page

Their own clicks and teammates' routine actions stay in the Activity Log only.
The sidebar count is the Inbox's unread count.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db.mongo import activity_entries, inbox_state
from app.shared.activity.store import (
    LANE_ACTIVE,
    LANE_PASSIVE,
    is_unread,
    unread_filter,
    visibility_filter,
)

WINDOW = timedelta(days=30)
LIMIT = 20
MAX_IDS = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state_id(workspace_id: str, user_id: str) -> str:
    return f"{workspace_id}:{user_id}"


async def read_before(workspace_id: str, user_id: str) -> Optional[datetime]:
    doc = await inbox_state.find_one({"_id": _state_id(workspace_id, user_id)}, {"read_before": 1}) or {}
    value = doc.get("read_before")
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def inbox_query(workspace_id: str, user_id: str, role: str) -> dict:
    query = visibility_filter(workspace_id, user_id, role)
    query["occurred_at"] = {"$gte": _now() - WINDOW}
    query["$and"] = [
        {"$or": [
            {"lane": LANE_ACTIVE},
            {"status": "failed"},
            {"actor.type": {"$in": ["ai_agent", "system_cron", "webhook"]}},
            {"category": "content_generated", "actor.user_id": user_id},
        ]},
        # Snoozed suggestions stay out until the snooze lapses.
        {"$or": [{"snoozed_until": None}, {"snoozed_until": {"$lte": _now()}}]},
    ]
    return query


async def unread_count(workspace_id: str, user_id: str, role: str) -> int:
    query = inbox_query(workspace_id, user_id, role)
    cursor = await read_before(workspace_id, user_id)
    query["$and"] = list(query["$and"]) + unread_filter(user_id, cursor)["$and"]
    return await activity_entries.count_documents(query)


async def list_inbox(workspace_id: str, user_id: str, role: str) -> dict:
    docs = await activity_entries.find(inbox_query(workspace_id, user_id, role)).sort(
        [("occurred_at", -1), ("_id", -1)]
    ).limit(LIMIT).to_list(length=LIMIT)
    cursor = await read_before(workspace_id, user_id)
    return {
        "docs": docs,
        "unread_ids": {d["_id"] for d in docs if is_unread(d, user_id, cursor)},
        "unread": await unread_count(workspace_id, user_id, role),
    }


async def set_read(workspace_id: str, user_id: str, ids: list[str], *, unread: bool = False) -> None:
    ids = ids[:MAX_IDS]
    add, pull = ("unread_by", "read_by") if unread else ("read_by", "unread_by")
    await activity_entries.update_many(
        {"_id": {"$in": ids}, "workspace_id": workspace_id},
        {"$addToSet": {add: user_id}, "$pull": {pull: user_id}},
    )


async def mark_all_read(workspace_id: str, user_id: str) -> None:
    await inbox_state.update_one(
        {"_id": _state_id(workspace_id, user_id)},
        {"$set": {"workspace_id": workspace_id, "user_id": user_id, "read_before": _now()}},
        upsert=True,
    )
    # Explicit "mark unread" flags are cleared too — "all" means all.
    await activity_entries.update_many(
        {"workspace_id": workspace_id, "unread_by": user_id},
        {"$pull": {"unread_by": user_id}},
    )


async def hide(workspace_id: str, user_id: str, ids: list[str]) -> int:
    """Delete from this member's log. Returns how many were removed; items
    still waiting on a decision are skipped."""
    res = await activity_entries.update_many(
        {"_id": {"$in": ids[:MAX_IDS]}, "workspace_id": workspace_id, "lane": LANE_PASSIVE},
        {"$addToSet": {"hidden_by": user_id}},
    )
    return res.modified_count
