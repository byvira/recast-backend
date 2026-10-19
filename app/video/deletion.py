"""Removing a video from the library, and everything made from it.

What goes with a library video: the file itself, its preview material (small copy, poster, thumbnail strip, waveform), the sample
pictures taken for covers, its transcript, its reading of the video, its dubs and caption tracks, and any review link that shows it
(with the comments and answers on it). Clips of it are taken out of the projects that use them, as one version each, so those projects
stay valid and the member can go back from it. A file that storage cannot remove right now is noted and retried by the daily clean-up,
so nothing is silently kept. What is not deleted: pictures the member made from it (covers) and posts that were already published.

A post still scheduled to go out with the video blocks the deletion until it is cancelled or sent, unless staff are taking the video
down. Staff can take a video down at once for a reason, which is recorded."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import content_pieces, get_db, media_assets
from app.shared.storage import cloudinary_file_ref

logger = logging.getLogger(__name__)

PREVIEW_URL_KEYS = ("proxy_url", "poster_url", "strip_url", "waveform_url", "scenes_url")


class DeletionBlocked(Exception):
    """The video cannot be deleted yet. The message is plain and safe to show."""

    def __init__(self, message: str, usage: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.usage = usage or {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def usage(workspace_id: str, media_id: str) -> dict[str, Any]:
    """Where a library video is used, so the member sees what deleting it would touch."""
    db = get_db()
    projects = []
    async for p in db["video_projects"].find({"workspace_id": workspace_id, "deleted": {"$ne": True}}, {"id": 1, "title": 1, "edit": 1}):
        edit = p.get("edit") or {}
        if any(c.get("media_id") == media_id for c in (edit.get("video") or []) + (edit.get("audio") or []) + (edit.get("shapes") or [])):
            projects.append({"id": p["id"], "title": p.get("title") or "Untitled"})
    posts = await content_pieces.find(
        {"workspace_id": workspace_id, "deleted": {"$ne": True}, "attachments.media_id": media_id}, {"piece_id": 1, "publish_status": 1, "platform": 1},
    ).to_list(length=200)
    scheduled = [p for p in posts if p.get("publish_status") in ("queued", "publishing")]
    links = await db["video_review_links"].count_documents({"workspace_id": workspace_id, "media_id": media_id, "revoked": {"$ne": True}})
    return {"projects": projects, "posts": len(posts), "scheduled_posts": len(scheduled), "review_links": links}


async def remove_file(url: str) -> str:
    """Remove one stored file. Returns "removed", "gone" (it was already not there), "not_ours" (not a file in our storage, nothing to
    do) or "error" (storage failed: keep it on the retry list)."""
    import asyncio

    import cloudinary.uploader

    from app.shared.storage import _ensure_configured

    ref = cloudinary_file_ref(url)
    if ref is None:
        return "not_ours"
    kind, public_id = ref
    try:
        _ensure_configured()
        result = await asyncio.to_thread(lambda: cloudinary.uploader.destroy(public_id, resource_type=kind, invalidate=True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not remove stored file %s: %s", public_id, exc)
        return "error"
    outcome = (result or {}).get("result")
    return "removed" if outcome == "ok" else "gone" if outcome == "not found" else "error"


def _file_urls(media: dict, frames: list[dict]) -> list[str]:
    urls = [media.get("url"), media.get("poster_url")]
    preview = media.get("preview") or {}
    urls += [preview.get(k) for k in PREVIEW_URL_KEYS]
    urls += [f.get("url") for f in frames]
    return [u for u in dict.fromkeys(urls) if isinstance(u, str) and u]


async def _remove_clips(workspace_id: str, media_id: str, actor: str) -> int:
    """Take clips of a deleted video out of every project that uses them, one new version each. Returns how many projects changed."""
    from app.api.v1.video_projects import _write_version
    from app.video import edl

    db = get_db()
    changed = 0
    async for found in db["video_projects"].find({"workspace_id": workspace_id, "deleted": {"$ne": True}}):
        p = found
        for _ in range(4):
            edit = p.get("edit") or {}
            keep = {k: [c for c in (edit.get(k) or []) if c.get("media_id") != media_id] for k in ("video", "audio", "shapes")}
            if all(len(keep[k]) == len(edit.get(k) or []) for k in keep):
                break
            new_edit = {**edit, **keep}
            ends = [c["at_f"] + c["dur_f"] for k in ("video", "audio") for c in keep[k]]
            new_edit["duration_f"] = max(ends + [1])
            try:
                new_edit = edl.validate(new_edit)
            except edl.EditError:
                break
            version = int(p.get("version", 1)) + 1
            # written only if the project is still at the version that was read, so a save the member made at the same moment is not overwritten
            res = await db["video_projects"].update_one(
                {"id": p["id"], "workspace_id": workspace_id, "version": p.get("version", 1)},
                {"$set": {"edit": new_edit, "version": version, "updated_at": _now()}},
            )
            if res.modified_count:
                await _write_version({**p, "edit": new_edit, "version": version}, "delete", "A video was deleted", actor)
                changed += 1
                break
            p = await db["video_projects"].find_one({"id": p["id"], "workspace_id": workspace_id, "deleted": {"$ne": True}})
            if not p:
                break
    return changed


async def delete_media(
    workspace_id: str, media_id: str, *, actor: str, staff_reason: Optional[str] = None, confirm: bool = False,
) -> dict[str, Any]:
    """Delete one library video and what was made from it. Raises DeletionBlocked. `staff_reason` marks a takedown: it needs no
    confirmation and ignores scheduled posts (they are cancelled), and the reason is recorded."""
    db = get_db()
    media = await media_assets.find_one({"id": media_id, "workspace_id": workspace_id})
    if not media:
        raise DeletionBlocked("That video was not found.")
    use = await usage(workspace_id, media_id)
    if not staff_reason:
        if use["scheduled_posts"]:
            raise DeletionBlocked(f"{use['scheduled_posts']} scheduled post{'s use' if use['scheduled_posts'] != 1 else ' uses'} this video. Cancel or send "
                                  "them first.", use)
        if (use["projects"] or use["review_links"]) and not confirm:
            raise DeletionBlocked("This video is used in your projects or shared for review. Deleting it removes it from them. Confirm to go on.", use)
    elif use["scheduled_posts"]:
        await content_pieces.update_many(
            {"workspace_id": workspace_id, "attachments.media_id": media_id, "publish_status": "queued"},
            {"$set": {"publish_status": "pending", "publish_scheduled_at": "", "publish_target": "", "hold": {"reason": "takedown", "at": _now()}}},
        )

    frames_doc = await db["video_cover_frames"].find_one({"workspace_id": workspace_id, "media_id": media_id}) or {}
    urls = _file_urls(media, frames_doc.get("frames") or [])
    removed, failed = 0, []
    for url in urls:
        outcome = await remove_file(url)
        if outcome in ("removed", "gone"):
            removed += 1
        elif outcome == "error":
            failed.append(url)                      # kept in a retry list, never forgotten
    if failed:
        await db["video_file_cleanup"].insert_many([{"id": uuid4().hex, "workspace_id": workspace_id, "url": u, "created_at": _now(), "attempts": 1} for u in failed])

    projects_changed = await _remove_clips(workspace_id, media_id, actor)
    tx_ids = [t["id"] async for t in db["video_transcripts"].find({"workspace_id": workspace_id, "media_id": media_id}, {"id": 1})]
    if tx_ids:
        await db["video_transcript_pages"].delete_many({"transcript_id": {"$in": tx_ids}})
        await db["video_transcript_chunks"].delete_many({"transcript_id": {"$in": tx_ids}})
    for name, field in (("video_transcripts", "media_id"), ("video_analysis", "media_id"), ("video_cover_frames", "media_id"), ("video_reframe_paths", "media_id"),
                        ("video_caption_tracks", "media_id"), ("video_dubs", "media_id")):
        await db[name].delete_many({"workspace_id": workspace_id, field: media_id})
    link_ids = [lk["id"] async for lk in db["video_review_links"].find({"workspace_id": workspace_id, "$or": [{"media_id": media_id}, {"compare_media_id": media_id}]}, {"id": 1})]
    if link_ids:
        for name in ("video_review_comments", "video_review_decisions"):
            await db[name].delete_many({"link_id": {"$in": link_ids}})
        await db["video_review_links"].delete_many({"id": {"$in": link_ids}})
    await media_assets.delete_one({"id": media_id, "workspace_id": workspace_id})
    record = {"id": uuid4().hex, "workspace_id": workspace_id, "media_id": media_id, "by": actor, "at": _now(), "takedown_reason": staff_reason,
              "files_removed": removed, "files_to_retry": len(failed), "projects_changed": projects_changed, "review_links_ended": len(link_ids)}
    await db["video_deletions"].insert_one(dict(record))
    return {k: record[k] for k in ("files_removed", "files_to_retry", "projects_changed", "review_links_ended")}


async def retry_failed_files(limit: int = 100) -> int:
    """Daily: try again the files storage could not remove. Gives up (and says so in the log) after five tries."""
    db = get_db()
    done = 0
    async for row in db["video_file_cleanup"].find({}).limit(limit):
        if await remove_file(row["url"]) != "error":
            await db["video_file_cleanup"].delete_one({"id": row["id"]})
            done += 1
            continue
        attempts = int(row.get("attempts", 1)) + 1
        if attempts >= 5:
            logger.error("Giving up removing %s after %d tries", row["url"], attempts)
            await db["video_file_cleanup"].delete_one({"id": row["id"]})
        else:
            await db["video_file_cleanup"].update_one({"id": row["id"]}, {"$set": {"attempts": attempts}})
    return done
