"""Video work that other agents and flows call directly, outside a request: render a project and wait for it."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from app.core.config import settings
from app.db.mongo import get_db, media_assets
from app.shared import pipeline_runs
from app.video import edl, jobs
from app.video.origin import summarise

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0
PRESETS = {
    "standard": {"crf": 21, "preset": "veryfast", "audio_bitrate": "192k"},
    "high": {"crf": 18, "preset": "faster", "audio_bitrate": "256k"},
    "small": {"crf": 27, "preset": "veryfast", "audio_bitrate": "128k"},
}


class RenderFailed(Exception):
    """The render ended without a video. The message is plain and safe to show."""


async def start_project_render(
    *, workspace_id: str, user_id: str, project_id: str, preset: str = "standard", lufs: float = -14.0, show_in_activity: bool = False,
) -> tuple[dict, dict]:
    """Queue a render of the project's current edit and return (job, project) without waiting. Raises RenderFailed with a plain message."""
    if preset not in PRESETS:
        raise RenderFailed("Preset must be standard, high or small.")
    project = await get_db()["video_projects"].find_one({"id": project_id, "workspace_id": workspace_id, "deleted": {"$ne": True}})
    if not project:
        raise RenderFailed("Video project not found.")
    ids = edl.media_ids(project["edit"])
    rows = await media_assets.find({"workspace_id": workspace_id, "id": {"$in": list(ids)}}).to_list(length=max(1, len(ids)))
    media = {}
    origins = []
    for r in rows:
        kind = r.get("kind")
        kind = kind.value if hasattr(kind, "value") else kind
        media[r["id"]] = {"url": r.get("url"), "kind": kind, "font_family": r.get("font_family")}
        from app.video.origin import origin_of

        origins.append(origin_of(r))
    try:
        compiled = edl.to_job(project["edit"], media, {**PRESETS[preset], "lufs": lufs})
        job = await jobs.enqueue(
            workspace_id=workspace_id, user_id=user_id, inputs=compiled["inputs"], edit=compiled["edit"], output=compiled["output"],
            label=f"Rendering {project.get('title') or 'video'}", project_id=project_id, show_in_activity=show_in_activity,
            href=f"/dashboard/pipelines/video?project={project_id}", origin=summarise(origins),
            idem_key=f"proj:{project_id}:v{project['version']}:{preset}:{lufs}",
        )
    except edl.EditError as exc:
        raise RenderFailed(str(exc))
    except jobs.JobError as exc:
        raise RenderFailed(exc.message)
    except pipeline_runs.RunAlreadyActive:
        raise RenderFailed("This video is already being rendered. It will show in your videos when it is ready.")
    return job, project


async def render_project_and_wait(
    *, workspace_id: str, user_id: str, project_id: str, preset: str = "standard", lufs: float = -14.0,
    reporter: Any = None, max_wait_s: Optional[float] = None, show_in_activity: bool = False,
) -> dict:
    """Queue a render of the project's current edit and wait for the finished video. Progress is passed to `reporter` as the
    worker reports it, so a director running this inside its own run shows one line of progress, not two."""
    job, project = await start_project_render(
        workspace_id=workspace_id, user_id=user_id, project_id=project_id, preset=preset, lufs=lufs, show_in_activity=show_in_activity,
    )
    try:
        done = await wait_for_job(workspace_id, job["id"], reporter=reporter, max_wait_s=max_wait_s)
    except asyncio.CancelledError:
        # a flow that was cancelled does not leave its render running in the queue (one that only ran out of waiting time does: it
        # carries on and shows in the activity list)
        try:
            await asyncio.shield(jobs.cancel_job(workspace_id, job["id"]))
        except Exception as exc:  # noqa: BLE001 - it may already have ended
            logger.debug("No render left to stop for %s: %s", job.get("id"), exc)
        raise
    await get_db()["video_projects"].update_one(
        {"id": project_id, "workspace_id": workspace_id},
        {"$set": {"last_render": {"job_id": job["id"], "media_id": done["media_id"], "preset": preset, "version": project["version"]}}},
    )
    return done


async def wait_for_job(workspace_id: str, job_id: str, *, reporter: Any = None, max_wait_s: Optional[float] = None) -> dict:
    """Wait for a render job to end. Each stage the worker reports is passed to `reporter`, so the run that is waiting shows one
    line of real progress. Raises RenderFailed with the job's own plain message when it does not end in a video."""
    limit = max_wait_s or float(settings.VIDEO_MAX_OUTPUT_SECONDS) * 2 + 600
    began = time.monotonic()
    last_stage = None
    while True:
        current = await jobs.get_job(workspace_id, job_id)
        if not current:
            raise RenderFailed("The render could not be found.")
        stage = (current.get("progress") or {}).get("stage")
        if reporter is not None and stage and stage != last_stage:
            await reporter.step(stage)
            last_stage = stage
        if current["status"] == "done":
            return {"job_id": job_id, "media_id": current["media_id"], "origin": current.get("origin"),
                    "duration_s": current.get("duration_s")}
        if current["status"] in ("failed", "cancelled", "blocked"):
            raise RenderFailed(current.get("error") or "The video was not made.")
        if time.monotonic() - began > limit:
            raise RenderFailed("The render is taking longer than expected. It is still in the queue; check back in the activity list.")
        await asyncio.sleep(POLL_SECONDS)
