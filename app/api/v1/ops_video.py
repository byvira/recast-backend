"""Ops view of the video render worker: whether it is on and alive, what is waiting, and a way to start one real render
from a video already in a workspace's library (used to prove the whole path end to end)."""
from typing import Any, Literal, Optional

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


@router.get("/usage")
@limiter.limit("30/minute")
async def usage_by_workspace(request: Request, limit: int = Query(20, ge=1, le=100), user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """This month's render minutes and failures per workspace, most minutes first, with each plan's limit. Cost drivers at a glance."""
    from app.db.mongo import get_db
    from app.video import limits

    db = get_db()
    rate = float(settings.VIDEO_RENDER_COST_PER_MINUTE or 0.0)
    since = limits.month_start()
    rows = await db["video_jobs"].aggregate([
        {"$match": {"created_at": {"$gte": since}, "kind": {"$in": [None, "render"]}}},
        {"$group": {
            "_id": "$workspace_id",
            "seconds": {"$sum": {"$cond": [{"$in": ["$status", ["queued", "claimed", "done"]]}, "$duration_s", 0]}},
            "renders": {"$sum": 1},
            "failed": {"$sum": {"$cond": [{"$in": ["$status", ["failed", "blocked"]]}, 1, 0]}},
        }},
        {"$sort": {"seconds": -1}}, {"$limit": limit},
    ]).to_list(length=limit)
    tiers = {w["id"]: w.get("tier") for w in await db["workspaces"].find({"id": {"$in": [r["_id"] for r in rows]}}, {"id": 1, "tier": 1}).to_list(length=limit)}
    out = []
    from app.shared import entitlements

    for r in rows:
        cap = (await entitlements.entitlements_for(r["_id"])).limit("render_minutes_month")
        minutes = round(r["seconds"] / 60, 1)
        out.append({"workspace_id": r["_id"], "tier": tiers.get(r["_id"]), "render_minutes": minutes, "limit_minutes": cap,
                    "renders": r["renders"], "failed": r["failed"], "estimated_cost_usd": round(minutes * rate, 2)})
    return {"month": since.strftime("%Y-%m"), "workspaces": out, "cost_per_minute_usd": rate,
            "estimated_cost_usd": round(sum(w["estimated_cost_usd"] for w in out), 2)}


class AccessBody(BaseModel):
    enabled: bool
    note: str = Field("", max_length=200)


@router.put("/workspaces/{workspace_id}/access")
@limiter.limit("30/minute")
async def set_workspace_access(request: Request, workspace_id: str, body: AccessBody, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Switch video on or off for one workspace: a closed-group rollout, or a workspace that has to be held. On by default."""
    from app.video import governance

    if not await governance.set_access(workspace_id, body.enabled, str(user.get("id")), body.note):
        raise HTTPException(status_code=404, detail="That workspace was not found.")
    return {"workspace_id": workspace_id, "enabled": body.enabled}


class TakedownBody(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=80)
    media_id: str = Field(..., min_length=1, max_length=80)
    reason: str = Field(..., min_length=3, max_length=500)


@router.post("/takedown")
@limiter.limit("20/minute")
async def takedown(request: Request, body: TakedownBody, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Take a video down at once: the file, everything made from it, every review link that shows it. The reason is recorded."""
    from app.video import deletion

    try:
        return await deletion.delete_media(body.workspace_id, body.media_id, actor=f"staff:{user.get('id')}", staff_reason=body.reason.strip())
    except deletion.DeletionBlocked as exc:
        raise HTTPException(status_code=404, detail=exc.message)


@router.get("/takedowns")
@limiter.limit("60/minute")
async def list_takedowns(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Reports made from review links, newest first. The link is already switched off when a report arrives."""
    from app.db.mongo import get_db

    rows = await get_db()["video_takedown_requests"].find({}).sort("at", -1).to_list(length=100)
    return {"requests": [{"id": r["id"], "workspace_id": r["workspace_id"], "media_id": r["media_id"], "reason": r["reason"], "contact": r.get("contact"),
                          "status": r["status"], "at": r["at"].isoformat().replace("+00:00", "Z") if r.get("at") else None} for r in rows]}


class ResolveBody(BaseModel):
    action: Literal["remove", "dismiss"]
    note: str = Field("", max_length=500)


@router.post("/takedowns/{request_id}/resolve")
@limiter.limit("30/minute")
async def resolve_takedown(request: Request, request_id: str, body: ResolveBody, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    from datetime import datetime, timezone

    from app.db.mongo import get_db
    from app.video import deletion

    row = await get_db()["video_takedown_requests"].find_one({"id": request_id})
    if not row or row["status"] != "open":
        raise HTTPException(status_code=404, detail="That report was not found or is already handled.")
    done: dict[str, Any] = {}
    if body.action == "remove":
        try:
            done = await deletion.delete_media(row["workspace_id"], row["media_id"], actor=f"staff:{user.get('id')}", staff_reason=row["reason"][:200])
        except deletion.DeletionBlocked:
            done = {"already_gone": True}
    await get_db()["video_takedown_requests"].update_one({"id": request_id}, {"$set": {
        "status": "removed" if body.action == "remove" else "dismissed", "note": body.note.strip(), "handled_by": user.get("id"), "handled_at": datetime.now(timezone.utc)}})
    return {"status": "removed" if body.action == "remove" else "dismissed", **done}


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
