"""Uploading a source video for editing.

The browser sends the file straight to storage in pieces, so a large video never passes through this server (512 MB of
memory) and a dropped connection resumes from the last piece. This API signs the upload, then checks what arrived: it is in
our storage under the name it was given, its size is what was declared, and it can really be read as a video.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.v1.media import ALLOWED_MIME_TYPES, _max_bytes_for
from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db, media_assets, workspaces
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
    from app.video import governance

    if not await governance.video_enabled(ctx.workspace_id):
        raise HTTPException(status_code=403, detail=governance.ACCESS_OFF)
    cap = await _limit_bytes(ctx.workspace_id)
    if body.bytes > cap:
        raise HTTPException(
            status_code=413,
            detail=f"This video is {body.bytes / (1024 * 1024):.0f} MB. For now a video can be up to {cap // (1024 * 1024)} MB.",
        )
    from app.video import limits

    ws_doc = await workspaces.find_one({"id": ctx.workspace_id}, {"tier": 1})
    room_mb = limits.limits_for(ws_doc)["storage_mb"]
    used_mb = await limits.storage_bytes_used(ctx.workspace_id) / 1_000_000.0
    if used_mb + body.bytes / 1_000_000.0 > room_mb:
        raise HTTPException(
            status_code=413,
            detail=f"Your videos take {used_mb:.0f} MB of the {room_mb} MB your plan includes, so this one does not fit. Remove videos you no longer need first.",
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


@router.get("/pictures")
@limiter.limit("60/minute")
async def library_pictures(
    request: Request, limit: int = Query(48, ge=1, le=100), ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """The workspace's pictures, newest first, for putting one over a video (a logo, a badge, a photo). Each carries where it came from."""
    from app.video.origin import origin_of

    rows = await media_assets.find(
        {"workspace_id": ctx.workspace_id, "kind": {"$in": ["image", MediaKind.IMAGE.value]}, "url": {"$type": "string"}},
        {"id": 1, "url": 1, "width": 1, "height": 1, "origin": 1, "contains_ai": 1, "contains_stock": 1, "source": 1, "created_at": 1, "title": 1},
    ).sort("created_at", -1).to_list(length=limit)
    return {"pictures": [{
        "id": r["id"], "url": r["url"], "width": r.get("width"), "height": r.get("height"), "title": r.get("title") or "",
        "origin": origin_of(r), "contains_ai": bool(r.get("contains_ai")), "contains_stock": bool(r.get("contains_stock")),
    } for r in rows]}


class LinkBody(BaseModel):
    link: str = Field(..., min_length=10, max_length=1000)


@router.post("/link-import", status_code=201)
@limiter.limit("6/minute")
async def import_from_link(request: Request, body: LinkBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Brings in a video from a Dropbox or Google Drive link that anyone with the link can open. Storage fetches the file itself."""
    import httpx

    from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url
    from app.video import governance, limits, link_import

    if not await governance.video_enabled(ctx.workspace_id):
        raise HTTPException(status_code=403, detail=governance.ACCESS_OFF)
    try:
        provider, direct = link_import.direct_address(body.link)
        await assert_safe_url(direct)
    except (link_import.LinkError, UnsafeUrl) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "That link cannot be used.") from exc

    cap = await _limit_bytes(ctx.workspace_id)
    # read only the headers first: where the link really leads, what it is, and how big. Each redirect is checked before it is followed,
    # so a link cannot lead this server on to an address it should never call.
    try:
        final, kind, declared, disposition, status = direct, "", 0, None, 0
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            for _hop in range(6):
                async with client.stream("GET", final) as res:
                    status = res.status_code
                    if status in (301, 302, 303, 307, 308) and res.headers.get("location"):
                        nxt = str(httpx.URL(final).join(res.headers["location"]))
                        if not link_import.fetch_host_ok(nxt):
                            raise HTTPException(status_code=400, detail="That link could not be opened. Check that anyone with the link can view it.")
                        await assert_safe_url(nxt)
                        final = nxt
                        continue
                    kind = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
                    length = (res.headers.get("content-length") or "").strip()
                    declared = int(length) if length.isdigit() else 0
                    disposition = res.headers.get("content-disposition")
                    break
            else:
                raise HTTPException(status_code=400, detail="That link sends you round in circles. Check the link.")
    except UnsafeUrl as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "That link cannot be used.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail="That link could not be reached. Check that anyone with the link can open it.") from exc
    if status != 200 or not link_import.fetch_host_ok(final):
        raise HTTPException(status_code=400, detail="That link could not be opened. Check that anyone with the link can view it.")
    if kind.startswith("text/html"):
        raise HTTPException(status_code=400, detail="That link opens a page, not the video. Share the file so anyone with the link can view it. A very large Drive file may not hand over directly.")
    if declared and declared > cap:
        raise HTTPException(status_code=413, detail=f"This video is {declared / (1024 * 1024):.0f} MB. For now a video can be up to {cap // (1024 * 1024)} MB.")
    ws_doc = await workspaces.find_one({"id": ctx.workspace_id}, {"tier": 1})
    room_mb = limits.limits_for(ws_doc)["storage_mb"]
    used_mb = await limits.storage_bytes_used(ctx.workspace_id) / 1_000_000.0
    if used_mb + declared / 1_000_000.0 > room_mb:
        raise HTTPException(status_code=413, detail=f"Your videos take {used_mb:.0f} MB of the {room_mb} MB your plan includes, so this one does not fit. Remove videos you no longer need first.")

    upload_id = str(uuid4())
    filename = link_import.name_from_headers(disposition, f"{provider}-video.mp4")
    try:
        params = storage_params.source_upload_params(workspace_id=ctx.workspace_id, upload_id=upload_id, filename=filename, nbytes=max(declared, 1))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    public_id = f"{params['folder']}/{params['public_id']}"
    try:
        import cloudinary.uploader

        from app.shared.storage import _ensure_configured

        _ensure_configured()
        stored = await asyncio.to_thread(lambda: cloudinary.uploader.upload(
            direct, resource_type="video", public_id=params["public_id"], folder=params["folder"], overwrite=True, timeout=600))
    except Exception as exc:  # noqa: BLE001 - the member gets a plain reason; the detail is logged
        logger.warning("Link import failed for %s: %s", ctx.workspace_id, exc)
        raise HTTPException(status_code=502, detail="The video could not be fetched from that link. Please try again, or upload the file instead.") from exc
    url = stored.get("secure_url") or ""
    size = int(stored.get("bytes") or 0)
    if not url.startswith(f"https://res.cloudinary.com/{settings.CLOUDINARY_CLOUD_NAME}/video/upload/") or size <= 0:
        await _destroy(public_id)
        raise HTTPException(status_code=502, detail="The video could not be fetched from that link. Please try again, or upload the file instead.")
    if size > cap or used_mb + size / 1_000_000.0 > room_mb:
        await _destroy(public_id)
        raise HTTPException(status_code=413, detail=f"This video is {size / (1024 * 1024):.0f} MB, which is more than your account can hold right now.")
    try:
        found = await probe_module.probe_source(url)
    except probe_module.ProbeError as exc:
        await _destroy(public_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = _now()
    video = found["video"]
    media = MediaAsset(
        id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.VIDEO, url=url, mime_type=kind if kind.startswith("video/") else "video/mp4",
        width=video["shown_width"], height=video["shown_height"], duration_s=found["duration_s"], source=MediaSource.UPLOADED,
        created_by=ctx.user_id, created_at=now, size_bytes=size, probe=found,
    )
    await media_assets.insert_one(media.model_dump())
    await _uploads().insert_one({
        "id": upload_id, "workspace_id": ctx.workspace_id, "user_id": ctx.user_id, "filename": filename[:200], "content_type": media.mime_type,
        "bytes": size, "status": "done", "public_id": public_id, "media_id": media.id, "url": url, "source": f"link:{provider}",
        "created_at": now, "finished_at": now,
    })
    try:
        from app.video.jobs import enqueue_preview

        await enqueue_preview(workspace_id=ctx.workspace_id, user_id=ctx.user_id, media_id=media.id, url=url, kind="video")
        await media_assets.update_one({"id": media.id}, {"$set": {"preview_status": "preparing"}})
    except Exception as exc:  # noqa: BLE001 - the video is saved either way
        logger.warning("Could not queue the preview for %s: %s", media.id, exc)
    return {"media": media.model_dump(mode="json"), "provider": provider, "notices": found["notices"]}


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
    if doc["status"] not in ("started", "completing"):
        raise HTTPException(status_code=409, detail="This upload was cancelled or has expired. Please start it again.")
    # only one request finishes an upload: a second one that arrives while the first is working is turned away instead of making a second item
    stale = _now() - timedelta(minutes=5)
    claimed = await _uploads().find_one_and_update(
        {"id": upload_id, "workspace_id": ctx.workspace_id, "$or": [{"status": "started"}, {"status": "completing", "completing_at": {"$lt": stale}}]},
        {"$set": {"status": "completing", "completing_at": _now()}},
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="This upload is already being finished. Give it a moment.")

    async def refuse(message: str, code: int = 400) -> HTTPException:
        await _uploads().update_one({"id": upload_id}, {"$set": {"status": "failed", "error": message}})
        await _destroy(doc.get("public_id"))
        return HTTPException(status_code=code, detail=message)

    # the address is read apart: the host and the folder must be ours and the file name must be this upload's, in the path itself and
    # not anywhere in the query or after a fragment
    parts = urlparse(body.url)
    wanted_name = f"/recast/video_source/{ctx.workspace_id}/{upload_id}_"
    if (
        parts.scheme != "https" or parts.netloc != "res.cloudinary.com" or parts.query or parts.fragment
        or not parts.path.startswith(f"/{settings.CLOUDINARY_CLOUD_NAME}/video/upload/") or wanted_name not in parts.path
    ):
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
    # the small playable copy, poster, thumbnails and waveform are made in the background; the editor says so until they are ready
    try:
        from app.video.jobs import enqueue_preview

        await enqueue_preview(workspace_id=ctx.workspace_id, user_id=ctx.user_id, media_id=media.id, url=body.url, kind="video")
        await media_assets.update_one({"id": media.id}, {"$set": {"preview_status": "preparing"}})
    except Exception as exc:  # noqa: BLE001 - the upload is saved either way
        logger.warning("Could not queue the preview for %s: %s", media.id, exc)
    return {"upload": _view({**doc, "status": "done", "media_id": media.id}), "media": media.model_dump(mode="json"), "notices": found["notices"]}


@router.post("/{media_id}/preview", status_code=202)
@limiter.limit("30/minute")
async def request_preview(request: Request, media_id: str, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    """Make the preview material for a library item that has none (an older upload). Safe to repeat."""
    from app.video.jobs import JobError, enqueue_preview

    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    if not media:
        raise HTTPException(status_code=404, detail="That item is not in this workspace's library.")
    kind = media.get("kind")
    kind = kind.value if hasattr(kind, "value") else kind
    if kind not in ("video", "audio", "image"):
        raise HTTPException(status_code=400, detail="Only videos, recordings and pictures have a preview.")
    if media.get("preview_status") == "ready":
        return {"media_id": media_id, "preview_status": "ready"}
    try:
        job = await enqueue_preview(workspace_id=ctx.workspace_id, user_id=ctx.user_id, media_id=media_id, url=media.get("url", ""), kind=kind)
    except JobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    await media_assets.update_one({"id": media_id}, {"$set": {"preview_status": "preparing"}})
    return {"media_id": media_id, "preview_status": "preparing", "job_id": job["id"]}


@router.get("/{media_id}/usage")
@limiter.limit("60/minute")
async def media_usage(request: Request, media_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """Where a library video is used: its projects, its posts and its review links. Shown before it is deleted."""
    from app.video import deletion

    return await deletion.usage(ctx.workspace_id, media_id)


@router.delete("/{media_id}")
@limiter.limit("20/minute")
async def delete_media_item(
    request: Request, media_id: str, confirm: bool = Query(False), ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict[str, Any]:
    """Delete a library video and everything made from it: its previews, sample pictures, transcript, reading, dubs, captions and review
    links. Clips of it are taken out of the projects that use them. A video used in projects or shared for review needs `confirm`; one
    still scheduled to go out in a post cannot be deleted until that post is cancelled or sent."""
    from app.video import deletion

    try:
        return await deletion.delete_media(ctx.workspace_id, media_id, actor=ctx.user_id, confirm=confirm)
    except deletion.DeletionBlocked as exc:
        status = 404 if exc.message.endswith("not found.") else 409
        raise HTTPException(status_code=status, detail={"message": exc.message, "usage": exc.usage})


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
