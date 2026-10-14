"""Removing a deleted workspace for good, once its hold period is over.

Deleting a workspace only hides it (see app.api.v1.workspace.delete_workspace) and keeps it for DELETE_HOLD_DAYS so a mistake can be
undone. This daily job then removes everything the workspace owned: its stored files, and every record in every collection that is
keyed by its id. Support tickets are kept (they are a record of what the person asked for and what staff answered).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_db, invites, media_assets, workspace_members, workspaces
from app.shared.storage import delete_file_by_url

logger = logging.getLogger(__name__)

#: Collections that keep their rows when a workspace is purged.
KEEP_PREFIXES = ("support_",)
#: At most this many workspaces are purged per run, so one run is never long.
MAX_PER_RUN = 3


async def purge_workspace(workspace_id: str) -> dict[str, int]:
    """Remove one workspace and everything it owned. Returns how many records were removed per collection."""
    # Stored files first, while the rows that point at them still exist. A file that cannot be removed is logged and left.
    async for media in media_assets.find({"workspace_id": workspace_id}, {"url": 1, "preview": 1, "poster_url": 1}):
        if media.get("url"):
            await delete_file_by_url(media["url"])
        # the preview files made from a video (small copy, poster, thumbnail strip, waveform) are stored separately
        for key, value in (media.get("preview") or {}).items():
            if key.endswith("_url") and isinstance(value, str):
                await delete_file_by_url(value)

    db = get_db()
    removed: dict[str, int] = {}
    for name in await db.list_collection_names():
        if name in ("workspaces", "users") or name.startswith(KEEP_PREFIXES):
            continue
        result = await db[name].delete_many({"workspace_id": workspace_id})
        if result.deleted_count:
            removed[name] = result.deleted_count
    await workspace_members.delete_many({"workspace_id": workspace_id})
    await invites.delete_many({"workspace_id": workspace_id})
    await workspaces.delete_one({"id": workspace_id})
    return removed


@distributed_job_lock("purge_deleted_workspaces", ttl_seconds=1800)
async def purge_deleted_workspaces() -> None:
    """Daily: purge the workspaces whose hold period has ended."""
    now = datetime.now(timezone.utc)
    due = await workspaces.find({"purge_after": {"$lt": now}}, {"id": 1}).to_list(length=MAX_PER_RUN)
    for ws in due:
        try:
            removed = await purge_workspace(ws["id"])
            logger.info("Purged deleted workspace %s: %s", ws["id"], removed)
        except Exception as exc:  # noqa: BLE001 - one workspace failing must not stop the others; it is tried again tomorrow
            logger.error("Could not purge workspace %s: %s", ws["id"], exc)
