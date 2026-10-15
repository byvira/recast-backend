"""What the video editor adds to the Activity Log: a finished video and a restored version. One row each, keyed so that a retry
updates the same row. Never raises: the log is a record, and a failed note must not fail a render or a restore."""

from __future__ import annotations

import logging
from typing import Optional

from app.db.mongo import get_db

logger = logging.getLogger(__name__)

HREF = "/dashboard/pipelines/video"


async def _title_of(workspace_id: str, project_id: str) -> str:
    doc = await get_db()["video_projects"].find_one({"id": project_id, "workspace_id": workspace_id}, {"title": 1})
    return ((doc or {}).get("title") or "Untitled video").strip()


async def video_made(workspace_id: str, user_id: Optional[str], project_id: Optional[str], job_id: str, *, duration_s: Optional[float],
                     origin: Optional[str] = None) -> None:
    if not project_id:
        return
    try:
        from app.shared.activity import record_system

        title = await _title_of(workspace_id, project_id)
        length = f" ({round(float(duration_s))} seconds)" if duration_s else ""
        await record_system(
            workspace_id=workspace_id, key=f"video_made:{job_id}", actor_name="Video editor", actor_user_id=user_id,
            category="content_generated", title=f"Made the video {title}", description=f"The finished video is ready{length}.",
            target_id=project_id, target_type="Video Project", target_label=title, href=f"{HREF}?project={project_id}",
            metadata={"jobId": job_id, "origin": origin} if origin else {"jobId": job_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Activity note for video %s failed: %s", job_id, exc)


async def _note(key: str, workspace_id: str, user_id: Optional[str], project_id: str, category: str, title_for, description: str,
                 status: str = "success", **meta) -> None:
    try:
        from app.shared.activity import record_system

        title = await _title_of(workspace_id, project_id)
        await record_system(
            workspace_id=workspace_id, key=key, actor_name="Video editor", actor_user_id=user_id, category=category,
            title=title_for(title), description=description, status=status, target_id=project_id, target_type="Video Project",
            target_label=title, href=f"{HREF}?project={project_id}", metadata=meta or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Activity note %s failed: %s", key, exc)


async def version_saved(workspace_id: str, user_id: Optional[str], project_id: str, version: int) -> None:
    """A save the member made on purpose. Autosaves are not noted: there would be one every few seconds."""
    await _note(f"video_save:{project_id}:{version}", workspace_id, user_id, project_id, "content_edited",
                lambda t: f"Saved {t}", f"Saved as version {version}.")


async def started_again(workspace_id: str, user_id: Optional[str], project_id: str, version: int) -> None:
    await _note(f"video_reset:{project_id}:{version}", workspace_id, user_id, project_id, "content_edited",
                lambda t: f"Started {t} again", f"Went back to how it began, saved as version {version}. The earlier versions are still there.")


_ENDED = {"failed": ("failed", "could not be made"), "cancelled": ("warning", "was cancelled"), "blocked": ("failed", "was held back by the safety check")}


async def render_ended(workspace_id: str, user_id: Optional[str], project_id: Optional[str], job_id: str, status: str, message: str) -> None:
    """A render that did not end in a video: failed, cancelled or held back. The reason is the plain one the member was shown."""
    if not project_id or status not in _ENDED:
        return
    tone, phrase = _ENDED[status]
    await _note(f"video_render_{status}:{job_id}", workspace_id, user_id, project_id, "content_generated",
                lambda t: f"The video {t} {phrase}", (message or "").strip()[:300] or "No reason was given.", status=tone, jobId=job_id)


async def version_restored(workspace_id: str, user_id: Optional[str], project_id: str, version: int, new_version: int) -> None:
    try:
        from app.shared.activity import record_system

        title = await _title_of(workspace_id, project_id)
        await record_system(
            workspace_id=workspace_id, key=f"video_restore:{project_id}:{new_version}", actor_name="Video editor", actor_user_id=user_id,
            category="content_edited", title=f"Restored {title} to version {version}",
            description=f"Saved as version {new_version}. Nothing was lost: the versions in between are still there.",
            target_id=project_id, target_type="Video Project", target_label=title, href=f"{HREF}?project={project_id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Activity note for the restore of %s failed: %s", project_id, exc)
