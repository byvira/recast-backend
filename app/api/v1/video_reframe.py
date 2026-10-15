"""Keep the speaker in frame: for a part of one video and a wanted shape, find where the middle of the crop window should sit over time. The
worker looks at the picture every fraction of a second and finds faces; this file starts that and reads back the result."""

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db, media_assets
from app.video import jobs

router = APIRouter()

STEP_S = 0.4


class ReframeBody(BaseModel):
    media_id: str = Field(..., max_length=64)
    #: the wanted shape, width over height (9:16 is 0.5625)
    aspect: float = Field(..., ge=0.2, le=5.0)
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., gt=0)


async def _video(ctx: WorkspaceContext, media_id: str) -> dict:
    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    kind = (media or {}).get("kind")
    kind = kind.value if hasattr(kind, "value") else kind
    if not media or kind != "video":
        raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
    return media


def _clamped_end(media: dict, end_s: float) -> float:
    """The end of the part, kept inside the video, so asking and answering always mean the same part."""
    duration = float(media.get("duration_s") or ((media.get("preview") or {}).get("meta") or {}).get("duration_s") or 0)
    return min(end_s, duration) if duration else end_s


def _spec_key(media_id: str, aspect: float, start_s: float, end_s: float) -> str:
    import hashlib
    import json

    spec = {"aspect": round(float(aspect), 4), "start_s": round(float(start_s), 2), "end_s": round(float(end_s), 2), "step_s": STEP_S}
    return f"reframe:{media_id}:{hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]}"


async def _state(ctx: WorkspaceContext, media_id: str, aspect: float, start_s: float, end_s: float) -> dict[str, Any]:
    key = _spec_key(media_id, aspect, start_s, end_s)
    doc = await get_db()["video_reframe_paths"].find_one({"workspace_id": ctx.workspace_id, "media_id": media_id, "key": key})
    if doc:
        note = None
        if not doc.get("faces_checked"):
            note = "The machine that prepares your videos has no face finder yet, so nothing was followed. The picture stays centred."
        elif doc.get("with_face", 0) == 0:
            note = "No face was found in this part, so the picture stays centred."
        return {"status": "done", "path": doc["path"], "window": doc["window"], "faces_checked": bool(doc.get("faces_checked")),
                "looked": doc.get("looked", 0), "with_face": doc.get("with_face", 0), "on_camera": doc.get("on_camera"), "is_estimate": True,
                "note": note}
    job = await get_db()["video_jobs"].find_one({"workspace_id": ctx.workspace_id, "idem_key": key})
    if job and job.get("status") in ("queued", "claimed"):
        return {"status": "running" if job["status"] == "claimed" else "queued", "path": [], "window": None, "faces_checked": False, "on_camera": None, "note": None}
    if job and job.get("status") == "failed":
        return {"status": "failed", "path": [], "window": None, "faces_checked": False, "note": job.get("error") or "The speaker could not be followed."}
    return {"status": "none", "path": [], "window": None, "faces_checked": False, "note": None}


@router.get("")
@limiter.limit("240/minute")
async def state(
    request: Request, media_id: str = Query(..., max_length=64), aspect: float = Query(..., ge=0.2, le=5.0),
    start_s: float = Query(..., ge=0), end_s: float = Query(..., gt=0), ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    media = await _video(ctx, media_id)
    return {"media_id": media_id, **await _state(ctx, media_id, aspect, start_s, _clamped_end(media, end_s))}


@router.post("", status_code=201)
@limiter.limit("30/minute")
async def start(request: Request, body: ReframeBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    media = await _video(ctx, body.media_id)
    preview = media.get("preview") or {}
    if media.get("preview_status") != "ready" or not preview.get("proxy_url"):
        raise HTTPException(status_code=409, detail="This video's preview is still being prepared. The speaker is followed from it.")
    end = _clamped_end(media, body.end_s)
    if end <= body.start_s:
        raise HTTPException(status_code=400, detail="That part is outside the video.")
    try:
        job = await jobs.enqueue_reframe(workspace_id=ctx.workspace_id, user_id=ctx.user_id, media_id=body.media_id, url=preview["proxy_url"],
                                         aspect=body.aspect, start_s=body.start_s, end_s=end, step_s=STEP_S)
    except jobs.JobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return {"job": {"id": job["id"], "status": job["status"]}, "end_s": end, "worker_ready": await jobs.worker_ready(),
            "started_at": datetime.now(timezone.utc).isoformat()}
