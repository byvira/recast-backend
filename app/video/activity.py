"""What the video editor adds to the Activity Log: a finished video and a restored version. One row each, keyed so that a retry
updates the same row. Never raises: the log is a record, and a failed note must not fail a render or a restore."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.mongo import get_db

logger = logging.getLogger(__name__)

HREF = "/dashboard/pipelines/video"


async def _title_of(workspace_id: str, project_id: str) -> str:
    doc = await get_db()["video_projects"].find_one({"id": project_id, "workspace_id": workspace_id}, {"title": 1})
    return ((doc or {}).get("title") or "Untitled video").strip()


async def emit(event: str, workspace_id: str, user_id: Optional[str], key: str, **payload) -> None:
    """One video event on the workspace bus, for Remy and Odette. `event` is the part after `video.`; `key` makes a redelivery a no-op.
    Never raises: the bus is a signal, and a failed one must not fail the work."""
    try:
        from app.models.agent_events import EventType
        from app.shared.events import emit_event
        from app.shared.pipeline_types import PipelineType

        await emit_event(
            event_type=EventType(f"video.{event}"), pipeline_type=PipelineType.VIDEO, workspace_id=workspace_id,
            actor_user_id=user_id or "system", payload={k: v for k, v in payload.items() if v is not None}, idempotency_key=f"video.{event}:{workspace_id}:{key}",
        )
        from app.video import webhooks

        await webhooks.dispatch(workspace_id, f"video.{event}", {k: v for k, v in payload.items() if v not in (None, "")})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Video event %s failed: %s", event, exc)


async def video_made(workspace_id: str, user_id: Optional[str], project_id: Optional[str], job_id: str, *, duration_s: Optional[float],
                     origin: Optional[str] = None) -> None:
    if not project_id:
        return
    await emit("render_done", workspace_id, user_id, job_id, project_id=project_id, job_id=job_id, duration_s=float(duration_s or 0), origin=origin or "")
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


async def approval_changed(workspace_id: str, user_id: Optional[str], project_id: str, version: int, approved: bool) -> None:
    """A sign-off given or taken away. Each version and answer is its own entry, so the log reads as a sequence."""
    word = "approved" if approved else "approval removed"
    await _note(f"video_approval:{project_id}:{version}:{word}:{int(datetime.now(timezone.utc).timestamp())}", workspace_id, user_id, project_id,
                "content_edited",
                lambda t: (f"Approved {t}" if approved else f"Took the approval off {t}"),
                f"Version {version} {'was signed off.' if approved else 'is no longer signed off.'}")


async def started_again(workspace_id: str, user_id: Optional[str], project_id: str, version: int) -> None:
    await _note(f"video_reset:{project_id}:{version}", workspace_id, user_id, project_id, "content_edited",
                lambda t: f"Started {t} again", f"Went back to how it began, saved as version {version}. The earlier versions are still there.")


_ENDED = {"failed": ("failed", "could not be made"), "cancelled": ("warning", "was cancelled"), "blocked": ("failed", "was held back by the safety check")}


async def render_ended(workspace_id: str, user_id: Optional[str], project_id: Optional[str], job_id: str, status: str, message: str) -> None:
    """A render that did not end in a video: failed, cancelled or held back. The reason is the plain one the member was shown."""
    if not project_id or status not in _ENDED:
        return
    tone, phrase = _ENDED[status]
    if status in ("failed", "blocked"):
        await emit("qc_failed", workspace_id, user_id, job_id, project_id=project_id, job_id=job_id, detail=(message or "")[:300])
    await _note(f"video_render_{status}:{job_id}", workspace_id, user_id, project_id, "content_generated",
                lambda t: f"The video {t} {phrase}", (message or "").strip()[:300] or "No reason was given.", status=tone, jobId=job_id)


async def review_answered(workspace_id: str, user_id: Optional[str], project_id: str, link_id: str, outcome: str, name: str, note: str) -> None:
    """A client approved the video or asked for changes on a review link. Said in the Activity Log and told to the member who shared it."""
    approved = outcome == "approved"
    try:
        from app.video import webhooks

        await webhooks.dispatch(workspace_id, "video.review_answered", {"project_id": project_id, "link_id": link_id, "outcome": outcome, "name": name, "note": note})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Webhook for the review answer failed: %s", exc)
    await _note(
        f"video_review:{link_id}:{outcome}:{name}", workspace_id, user_id, project_id, "content_edited",
        lambda t: f"{name} approved {t}" if approved else f"{name} asked for changes to {t}",
        (note or ("Approved with no notes." if approved else "No note was left.")), status="success" if approved else "warning",
        linkId=link_id, outcome=outcome,
    )


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
