"""Notes left on work that has since gone.

A note belongs to a post, a recording, a picture or a video project. When that work is removed the notes should go too, but work is first
only hidden (so a mistake can be undone). This daily job removes a note once its work is gone for good, or has been hidden for longer
than the hold, so notes never pile up on things nobody can open.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import audio_assets, content_pieces, get_db, image_assets

logger = logging.getLogger(__name__)

#: How long hidden work keeps its notes, in case it is brought back.
HOLD_DAYS = 30
#: Notes looked at per kind in one run, so a run is never long; the rest are looked at tomorrow.
BATCH = 500

_SOURCES = {
    "piece": (content_pieces, "piece_id"),
    "audio": (audio_assets, "id"),
    "image": (image_assets, "id"),
}


def removable(note_ids_by_target: dict[str, list[str]], alive: set[str], hidden: set[str], old: set[str]) -> list[str]:
    """The notes to remove: those on work that no longer exists, and those on hidden work whose note is older than the hold."""
    out: list[str] = []
    for target, ids in note_ids_by_target.items():
        if target in alive:
            continue
        if target in hidden:
            out.extend(i for i in ids if i in old)
        else:
            out.extend(ids)
    return out


async def purge_orphan_notes() -> dict[str, int]:
    notes = get_db()["editor_comments"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=HOLD_DAYS)
    removed: dict[str, int] = {}
    sources = {**_SOURCES, "video": (get_db()["video_projects"], "id")}
    for kind, (collection, key) in sources.items():
        rows = await notes.find({"target_type": kind}, {"id": 1, "target_id": 1, "workspace_id": 1, "created_at": 1}).limit(BATCH).to_list(length=BATCH)
        if not rows:
            continue
        by_target: dict[str, list[str]] = {}
        old: set[str] = set()
        for r in rows:
            by_target.setdefault(r["target_id"], []).append(r["id"])
            created = r.get("created_at")
            if isinstance(created, datetime) and (created if created.tzinfo else created.replace(tzinfo=timezone.utc)) < cutoff:
                old.add(r["id"])
        found = await collection.find({key: {"$in": list(by_target)}}, {key: 1, "deleted": 1}).to_list(length=len(by_target))
        alive = {f[key] for f in found if not f.get("deleted")}
        hidden = {f[key] for f in found if f.get("deleted")}
        gone = removable(by_target, alive, hidden, old)
        if gone:
            await notes.delete_many({"id": {"$in": gone}})
        removed[kind] = len(gone)
    return removed


@distributed_job_lock("purge_orphan_notes", ttl_seconds=900)
async def purge_orphan_notes_job() -> None:
    """Daily."""
    try:
        removed = await purge_orphan_notes()
        if any(removed.values()):
            logger.info("Removed notes on work that is gone: %s", removed)
    except Exception as exc:  # noqa: BLE001 - tried again tomorrow
        logger.error("Could not clear notes on removed work: %s", exc)
