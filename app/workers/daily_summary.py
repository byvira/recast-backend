"""A short note each morning about what happened in a workspace the day before.

Each workspace that had activity gets one row in its Activity Log: how many posts went out, how many failed, what Remy and Odette
found, and what is still waiting for a person. It is plain counting of work already recorded, so it costs nothing and calls no model.
A quiet day writes nothing, so the log never fills with empty notes. The row is keyed by workspace and day, so running the job twice
(or from two servers) writes one row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_db
from app.shared.activity import record_system
from app.shared.activity.i18n import phrase

logger = logging.getLogger(__name__)

#: Workspaces looked at in one run, and the least activity worth a note.
MAX_WORKSPACES = 3000
MIN_ENTRIES = 3


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary_text(c: dict[str, int]) -> str:
    """The note's body in plain English, from the day's counts. Only what happened is mentioned."""
    lines: list[str] = []
    if c["published"]:
        lines.append(f"{_plural(c['published'], 'post', 'posts')} went out.")
    if c["failed"]:
        lines.append(f"{_plural(c['failed'], 'thing', 'things')} did not work and may need a look.")
    if c["remy"]:
        lines.append(f"Remy had {_plural(c['remy'], 'suggestion', 'suggestions')} about your voice and writing.")
    if c["odette"]:
        lines.append(f"Odette raised {_plural(c['odette'], 'note', 'notes')} about your publishing.")
    if c["waiting"]:
        lines.append(f"{_plural(c['waiting'], 'item is', 'items are')} waiting for you to decide.")
    if not lines:
        lines.append(f"{_plural(c['entries'], 'thing', 'things')} happened, all of them fine.")
    return " ".join(lines)


async def counts_for(workspace_id: str, start: datetime, end: datetime) -> dict[str, int]:
    """What the day held, counted from the Activity Log and the posts themselves."""
    db = get_db()
    window = {"workspace_id": workspace_id, "occurred_at": {"$gte": start, "$lt": end}}
    kinds: dict[str, int] = {}
    async for row in db["activity_entries"].aggregate([
        {"$match": window}, {"$group": {"_id": {"kind": "$source.kind", "status": "$status"}, "n": {"$sum": 1}}},
    ]):
        key = row["_id"]
        kinds[f"{key.get('kind')}|{key.get('status')}"] = int(row["n"])
    by_kind = lambda kind: sum(n for k, n in kinds.items() if k.split("|")[0] == kind)  # noqa: E731
    return {
        "entries": sum(kinds.values()),
        "published": await db["content_pieces"].count_documents({"workspace_id": workspace_id, "publish_status": "published", "published_at": {"$gte": start, "$lt": end}}),
        "failed": sum(n for k, n in kinds.items() if k.split("|")[1] == "failed"),
        "remy": by_kind("remy_signal"),
        "odette": by_kind("odette_insight") + by_kind("odette_flag"),
        # a decided suggestion leaves the active lane, so what is still there is waiting for a person
        "waiting": await db["activity_entries"].count_documents({"workspace_id": workspace_id, "lane": "active", "occurred_at": {"$lt": end}}),
    }


async def write_summary(workspace_id: str, day: datetime) -> bool:
    """Write the note for the day starting at ``day`` (UTC midnight). False when the day was too quiet for one."""
    end = day + timedelta(days=1)
    c = await counts_for(workspace_id, day, end)
    if c["entries"] < MIN_ENTRIES:
        return False
    await record_system(
        workspace_id=workspace_id, key=f"daily-summary:{workspace_id}:{day.date().isoformat()}", actor_name="Ekavo", category="daily_summary",
        title=phrase("Yesterday, {day}", day=day.strftime("%d %b")), description=summary_text(c),
        status="warning" if c["failed"] else "success", occurred_at=end - timedelta(seconds=1), href="/dashboard/activity",
        metadata={"counts": c},
    )
    return True


@distributed_job_lock("daily_summary", ttl_seconds=1800)
async def write_daily_summaries() -> int:
    """Called once a morning. Writes yesterday's note for every workspace that had enough activity; returns how many."""
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    written = 0
    ids: list[Any] = await get_db()["activity_entries"].distinct("workspace_id", {"occurred_at": {"$gte": yesterday, "$lt": today}})
    for workspace_id in ids[:MAX_WORKSPACES]:
        try:
            if workspace_id and await write_summary(workspace_id, yesterday):
                written += 1
        except Exception as exc:  # noqa: BLE001 - one workspace's problem never stops the others
            logger.warning("Could not write the daily summary for %s: %s", workspace_id, exc)
    return written
