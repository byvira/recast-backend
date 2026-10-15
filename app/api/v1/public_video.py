"""The public video API for studios and scripts. It is called with a workspace key (`Authorization: Bearer ek_live_...`), never a login.
Reading needs the `video:read` scope; starting a render also needs `video:render`, and counts against the workspace's render minutes
like any render. Every key has its own rate limit. A missing, wrong or revoked key gets the same plain 401."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.middleware import limiter
from app.db.mongo import get_db
from app.video import api_keys, jobs, service

router = APIRouter()

UNAUTHORIZED = "A valid API key is needed. Send it as Authorization: Bearer <key>."


class ApiContext:
    def __init__(self, doc: dict):
        self.workspace_id: str = doc["workspace_id"]
        self.key_id: str = doc["id"]
        self.scopes: list[str] = doc["scopes"]
        self.user_id: str = doc.get("created_by") or "api"


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _limit_key(request: Request) -> str:
    """One rate limit per key; a request with no key shares one bucket per address, so guessing is slow."""
    token = _bearer(request)
    return f"key:{token[:24]}" if token else f"ip:{request.client.host if request.client else 'unknown'}"


async def api_context(request: Request) -> ApiContext:
    doc = await api_keys.authenticate(_bearer(request))
    if not doc:
        raise HTTPException(status_code=401, detail=UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    return ApiContext(doc)


def _need(ctx: ApiContext, scope: str) -> None:
    if scope not in ctx.scopes:
        raise HTTPException(status_code=403, detail=f"This key does not have the {scope} permission.")


def _iso(v: Any) -> Any:
    from datetime import timezone

    return v.replace(tzinfo=v.tzinfo or timezone.utc).isoformat().replace("+00:00", "Z") if hasattr(v, "isoformat") else None


def _project(d: dict) -> dict[str, Any]:
    edit = d.get("edit") or {}
    fps = edit.get("fps") or {"num": 30, "den": 1}
    last = d.get("last_render") or {}
    return {"id": d["id"], "title": d.get("title"), "version": d.get("version"), "width": edit.get("width"), "height": edit.get("height"),
            "duration_s": round((edit.get("duration_f") or 0) * fps["den"] / fps["num"], 2), "updated_at": _iso(d.get("updated_at")),
            "last_render": {"job_id": last.get("job_id"), "preset": last.get("preset"), "version": last.get("version")} if last else None}


async def _load(ctx: ApiContext, project_id: str) -> dict:
    doc = await get_db()["video_projects"].find_one({"id": project_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}})
    if not doc:
        raise HTTPException(status_code=404, detail="Video project not found.")
    return doc


@router.get("/projects")
@limiter.limit("60/minute", key_func=_limit_key)
async def list_projects(request: Request, ctx: ApiContext = Depends(api_context)) -> dict[str, Any]:
    _need(ctx, "video:read")
    rows = await get_db()["video_projects"].find({"workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}).sort("updated_at", -1).to_list(length=100)
    return {"projects": [_project(r) for r in rows]}


@router.get("/projects/{project_id}")
@limiter.limit("120/minute", key_func=_limit_key)
async def get_project(request: Request, project_id: str, ctx: ApiContext = Depends(api_context)) -> dict[str, Any]:
    _need(ctx, "video:read")
    return _project(await _load(ctx, project_id))


@router.get("/projects/{project_id}/handoff/{fmt}")
@limiter.limit("30/minute", key_func=_limit_key)
async def handoff(request: Request, project_id: str, fmt: str, ctx: ApiContext = Depends(api_context)) -> Response:
    """The same files as Send to editor: premiere, finalcut, edl, otio, markers, srt, vtt or package."""
    from app.api.v1 import video_projects

    _need(ctx, "video:read")
    doc = await _load(ctx, project_id) if fmt in video_projects.HANDOFF_FORMATS else None
    return await video_projects.handoff_response(ctx.workspace_id, doc, fmt)


class RenderBody(BaseModel):
    preset: Literal["standard", "high", "small"] = "standard"
    lufs: float = -14.0


@router.post("/projects/{project_id}/render", status_code=202)
@limiter.limit("10/minute", key_func=_limit_key)
async def start_render(request: Request, project_id: str, body: RenderBody, ctx: ApiContext = Depends(api_context)) -> dict[str, Any]:
    """Queue a render of the project as it is now. Returns the job at once; poll it, or register a webhook for video.render_done."""
    _need(ctx, "video:render")
    await _load(ctx, project_id)
    try:
        job, _ = await service.start_project_render(workspace_id=ctx.workspace_id, user_id=ctx.user_id, project_id=project_id, preset=body.preset, lufs=body.lufs)
    except service.RenderFailed as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"job_id": job["id"], "status": job["status"]}


@router.get("/jobs/{job_id}")
@limiter.limit("240/minute", key_func=_limit_key)
async def get_job(request: Request, job_id: str, ctx: ApiContext = Depends(api_context)) -> dict[str, Any]:
    _need(ctx, "video:read")
    job = await jobs.get_job(ctx.workspace_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Render not found.")
    keep = ("id", "status", "label", "error", "progress", "created_at", "finished_at", "project_id", "output", "quality")
    return {k: job.get(k) for k in keep if k in job}
