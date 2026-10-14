"""Uploading a source video for editing.

The browser sends the file straight to storage in pieces, so a large video never passes through this server (512 MB of
memory) and a dropped connection resumes from the last piece. This API signs the upload, then checks what arrived: it is in
our storage under the name it was given, its size is what was declared, and it can really be read as a video.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.v1.media import ALLOWED_MIME_TYPES, _max_bytes_for
from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import probe as probe_module
from app.video import storage_params
from app.video.jobs import _remote_size

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_OPEN_UPLOADS = 5


def _uploads():
    return get_db()["video_uploads"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _view(doc: dict) -> dict:
    out = {k: doc.get(k) for k in ("id", "status", "filename", "bytes", "media_id", "error")}
    for field in ("created_at", "expires_at"):
        value = doc.get(field)
        if value is not None:
            value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            out[field] = value.isoformat().replace("+00:00", "Z")
    return out


async def _limit_bytes(workspace_id: str) -> int:
    plan = await _max_bytes_for(MediaKind.VIDEO, workspace_id)
    return min(plan, int(settings.VIDEO_UPLOAD_MAX_MB) * 1024 * 1024)


async def _destroy(public_id: Optional[str]) -> None:
    if not public_id:
        return
    try:
        import cloudinary.uploader

        from app.shared.storage import _ensure_configured

        _ensure_configured()
        await asyncio.to_thread(lambda: cloudinary.uploader.destroy(public_id, resource_type="video"))
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
        logger.warning("Could not remove abandoned upload %s: %s", public_id, exc)


async def _sweep_abandoned(workspace_id: str) -> None:
    """Uploads that were started and never finished are closed and their half-sent files removed."""
    cutoff = _now()
    stale = await _uploads().find({"workspace_id": workspace_id, "status": "started", "expires_at": {"$lt": cutoff}}).to_list(length=20)
    for doc in stale:
        await _uploads().update_one({"id": doc["id"], "status": "started"}, {"$set": {"status": "expired"}})
        await _destroy(doc.get("public_id"))


class StartBody(BaseModel):
    filename: str = Field(..., min_length=1, max_length=200)
    content_type: str = Field(..., max_length=100)
    bytes: int = Field(..., gt=0)


class CompleteBody(BaseModel):
    url: str = Field(..., max_length=1000)
    bytes: int = Field(..., gt=0)


@router.get("/limits")
@limiter.limit("60/minute")
async def limits(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """What a source upload may be, for the upload screen to show before the member picks a file."""
    cap = await _limit_bytes(ctx.workspace_id)
    return {
        "max_bytes": cap, "max_mb": cap // (1024 * 1024), "content_types": sorted(ALLOWED_MIME_TYPES[MediaKind.VIDEO]),
        "expire_hours": settings.VIDEO_UPLOAD_EXPIRE_HOURS, "max_open": MAX_OPEN_UPLOADS,
    }


@router.post("/uploads", status_code=201)
@limiter.limit("30/minute")
async def start_upload(request: Request, body: StartBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    if body.content_type not in ALLOWED_MIME_TYPES[MediaKind.VIDEO]:
        raise HTTPException(status_code=400, detail="That kind of file can't be used as a video. Use MP4, MOV, WebM, AVI or FLV.")
    cap = await _limit_bytes(ctx.workspace_id)
    if body.bytes > cap:
        raise HTTPException(
            status_code=413,
            detail=f"This video is {body.bytes / (1024 * 1024):.0f} MB. For now a video can be up to {cap // (1024 * 1024)} MB.",
        )
    await _sweep_abandoned(ctx.workspace_id)
    open_now = await _uploads().count_documents({"workspace_id": ctx.workspace_id, "status": "started"})
    if open_now >= MAX_OPEN_UPLOADS:
        raise HTTPException(status_code=429, detail="Several uploads are already in progress. Let one finish or cancel it first.")

    upload_id = str(uuid4())
    try:
        params = storage_params.source_upload_params(
            workspace_id=ctx.workspace_id, upload_id=upload_id, filename=body.filename, nbytes=body.bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    now = _now()
    doc = {
        "id": upload_id, "workspace_id": ctx.workspace_id, "user_id": ctx.user_id, "filename": body.filename[:200],
        "content_type": body.content_type, "bytes": body.bytes, "status": "started", "public_id": f"{params['folder']}/{params['public_id']}",
        "created_at": now, "expires_at": now + timedelta(hours=int(settings.VIDEO_UPLOAD_EXPIRE_HOURS)),
    }
    await _uploads().insert_one(dict(doc))
    return {**_view(doc), "upload": {k: params[k] for k in ("method", "url", "fields", "file_field", "chunk_bytes")}, "unique_upload_id": upload_id}


@router.get("/uploads/{upload_id}")
@limiter.limit("120/minute")
async def get_upload(request: Request, upload_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    doc = await _uploads().find_one({"id": upload_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Upload not found.")
    return _view(doc)


@router.post("/uploads/{upload_id}/complete")
@limiter.limit("30/minute")
async def complete_upload(
    request: Request, upload_id: str, body: CompleteBody, ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    doc = await _uploads().find_one({"id": upload_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Upload not found.")
    if doc["status"] == "done":
        media = await media_assets.find_one({"id": doc["media_id"], "workspace_id": ctx.workspace_id})
        return {"upload": _view(doc), "media": MediaAsset(**media).model_dump(mode="json") if media else None}
    if doc["status"] != "started":
        raise HTTPException(status_code=409, detail="This upload was cancelled or has expired. Please start it again.")

    async def refuse(message: str, code: int = 400) -> HTTPException:
        await _uploads().update_one({"id": upload_id}, {"$set": {"status": "failed", "error": message}})
        await _destroy(doc.get("public_id"))
        return HTTPException(status_code=code, detail=message)

    expected = f"https://res.cloudinary.com/{settings.CLOUDINARY_CLOUD_NAME}/video/upload/"
    wanted_name = f"/recast/video_source/{ctx.workspace_id}/{upload_id}_"
    if not body.url.startswith(expected) or wanted_name not in body.url:
        raise await refuse("That file isn't the one this upload was made for.")
    if body.bytes != doc["bytes"]:
        raise await refuse("The file that arrived is not the size that was chosen. Please upload it again.")
    stored = await _remote_size(body.url)
    if stored is None or stored != doc["bytes"]:
        raise await refuse("The video didn't arrive completely. Please upload it again.")
    try:
        found = await probe_module.probe_source(body.url)
    except probe_module.ProbeError as exc:
        raise await refuse(str(exc))

    now = _now()
    video = found["video"]
    media = MediaAsset(
        id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.VIDEO, url=body.url, mime_type=doc["content_type"],
        width=video["shown_width"], height=video["shown_height"], duration_s=found["duration_s"], source=MediaSource.UPLOADED,
        created_by=ctx.user_id, created_at=now, size_bytes=doc["bytes"], probe=found,
    )
    await media_assets.insert_one(media.model_dump())
    await _uploads().update_one({"id": upload_id}, {"$set": {"status": "done", "media_id": media.id, "url": body.url, "finished_at": now}})
    return {"upload": _view({**doc, "status": "done", "media_id": media.id}), "media": media.model_dump(mode="json"), "notices": found["notices"]}


@router.post("/uploads/{upload_id}/cancel")
@limiter.limit("30/minute")
async def cancel_upload(request: Request, upload_id: str, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    doc = await _uploads().find_one_and_update(
        {"id": upload_id, "workspace_id": ctx.workspace_id, "status": "started"}, {"$set": {"status": "cancelled"}},
    )
    if not doc:
        raise HTTPException(status_code=409, detail="This upload has already finished or was cancelled.")
    await _destroy(doc.get("public_id"))
    return {"id": upload_id, "status": "cancelled"}
