"""Ops view of the video render worker: whether it is on and alive, what is waiting, and a way to start one real render
from a video already in a workspace's library (used to prove the whole path end to end)."""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import require_platform_staff
from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require_ops_admin
from app.db.mongo import media_assets
from app.video import jobs

router = APIRouter()

_ADMIN = require_ops_admin("manage_workspace_settings")


def _raise(exc: jobs.JobError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get("/status")
@limiter.limit("60/minute")
async def status(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return await jobs.worker_status()


@router.get("/jobs")
@limiter.limit("60/minute")
async def list_jobs(request: Request, limit: int = Query(20, ge=1, le=100), user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return {"jobs": await jobs.list_jobs(None, limit)}


class TestJobBody(BaseModel):
    media_id: str
    width: int = Field(1280, ge=16, le=4096)
    height: int = Field(720, ge=16, le=4096)
    fps: int = Field(30, ge=1, le=120)
    seconds: Optional[float] = Field(None, gt=0)


@router.post("/jobs/test")
@limiter.limit("6/minute")
async def start_test_job(request: Request, body: TestJobBody, ctx: WorkspaceContext = Depends(_ADMIN)) -> dict[str, Any]:
    """One plain render of a library video (its own sound kept, no captions), to prove the worker, storage and checks."""
    media = await media_assets.find_one({"id": body.media_id, "workspace_id": ctx.workspace_id, "kind": "video"})
    if not media or not str(media.get("url", "")).startswith("https://"):
        raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
    length = float(media.get("duration_s") or 0)
    if length <= 0 and body.seconds is None:
        raise HTTPException(status_code=400, detail="This video's length is not known, so give the seconds to render.")
    seconds = min(body.seconds or length, length or body.seconds, float(settings.VIDEO_MAX_OUTPUT_SECONDS))
    frames = max(1, int(seconds * body.fps))
    edit = {
        "version": 1, "fps": {"num": body.fps, "den": 1}, "width": body.width - body.width % 2, "height": body.height - body.height % 2,
        "duration_f": frames, "background": "#000000",
        "video": [{"id": "v1", "src": "cam", "at_f": 0, "dur_f": frames, "src_in_f": 0, "fit": "contain", "audio": True}],
        "audio": [], "text": [], "captions": {"enabled": False},
    }
    try:
        return await jobs.enqueue(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, inputs={"cam": {"url": media["url"], "kind": "video"}},
            edit=edit, label="Ops test render",
        )
    except jobs.JobError as exc:
        raise _raise(exc)


@router.post("/jobs/{job_id}/cancel")
@limiter.limit("30/minute")
async def cancel(request: Request, job_id: str, ctx: WorkspaceContext = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return await jobs.cancel_job(ctx.workspace_id, job_id)
    except jobs.JobError as exc:
        raise _raise(exc)
