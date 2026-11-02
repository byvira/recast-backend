"""Ops, Website content: blog posts, job posts, the pricing page words, the questions page and the legal pages.

Reading needs View on Website content, writing a draft and sending it for review needs Edit, and putting anything live, setting a
time, taking it down or deleting it needs Approve. The legal pages go live only by a Master Admin.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from app.core import staff as ST
from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.staff import require_action
from app.site_content import rules as R
from app.site_content import store as C

router = APIRouter()
PAGE = 50


class CreateBody(BaseModel):
    kind: Literal["post", "job", "page"]
    key: Optional[str] = Field(None, max_length=40)
    data: dict[str, Any] = Field(default_factory=dict)


class DraftBody(BaseModel):
    data: dict[str, Any]
    base_version: int = Field(..., ge=1)


class NoteBody(BaseModel):
    note: str = Field("", max_length=500)


class PublishBody(BaseModel):
    at: Optional[datetime] = None
    note: str = Field("", max_length=500)


class RestoreBody(BaseModel):
    version: int = Field(..., ge=1)


async def _legal_check(doc: dict[str, Any], user: dict[str, Any]) -> None:
    if doc["kind"] == "page" and doc["key"] in R.LEGAL and (await ST.access_for(user)).get("level") != "master_admin":
        raise HTTPException(status_code=403, detail="Only the Master Admin can put a legal page live. Send it for review instead.")


@router.get("")
@limiter.limit("60/minute")
async def list_content(
    request: Request,
    kind: Literal["post", "job", "page"] = "post",
    status: Optional[Literal["draft", "review", "scheduled", "live"]] = None,
    q: Optional[str] = Query(None, max_length=100),
    page: int = Query(0, ge=0, le=500),
) -> dict:
    await C.promote_due()
    query: dict[str, Any] = {"kind": kind, "deleted": {"$ne": True}}
    if q and q.strip():
        term = re.escape(q.strip())
        query["$or"] = [{"draft.title": {"$regex": term, "$options": "i"}}, {"key": {"$regex": term, "$options": "i"}}]
    if status == "review":
        query["review"] = {"$ne": None}
    elif status == "scheduled":
        query["schedule_at"] = {"$ne": None}
    cursor = C.content_col().find(query, {"_id": 0, "versions": 0}).sort("updated_at", -1)
    if status in ("draft", "live"):
        # worked out from the draft and live copies, so filtered here; the lists are small
        rows = [C.row(d) async for d in cursor.limit(2000)]
        rows = [r for r in rows if r["status"] == status]
        chunk = rows[page * PAGE:(page + 1) * PAGE]
        return {"items": chunk, "page": page, "more": len(rows) > (page + 1) * PAGE}
    docs = await cursor.skip(page * PAGE).limit(PAGE).to_list(PAGE)
    return {"items": [C.row(d) for d in docs], "page": page, "more": len(docs) == PAGE}


@router.get("/summary")
@limiter.limit("60/minute")
async def summary(request: Request) -> dict:
    """How many are waiting for review and set for later, for the counts on the tabs; and which pages have been started."""
    await C.promote_due()
    waiting: dict[str, int] = {}
    async for r in C.content_col().aggregate([
        {"$match": {"deleted": {"$ne": True}, "review": {"$ne": None}}}, {"$group": {"_id": "$kind", "n": {"$sum": 1}}},
    ]):
        waiting[r["_id"]] = int(r["n"])
    pages = {d["key"]: C.row(d) async for d in C.content_col().find({"kind": "page", "deleted": {"$ne": True}}, {"_id": 0})}
    return {"review": waiting, "pages": pages}


@router.get("/page/{key}")
@limiter.limit("60/minute")
async def page_item(request: Request, key: str) -> dict:
    if key not in R.PAGES:
        raise HTTPException(status_code=404, detail="That page isn't edited from Ops.")
    doc = await C.page_doc(key)
    return {"item": C.full(doc) if doc else None}


@router.post("")
@limiter.limit("30/minute")
async def create(request: Request, body: CreateBody, user: dict = Depends(get_current_user)) -> dict:
    doc = await C.create(body.kind, body.key, body.data, user)
    await ST.audit(user, "content.created", doc["id"], after={"kind": doc["kind"], "key": doc["key"]})
    return {"item": C.full(doc)}


@router.post("/media")
@limiter.limit("30/minute")
async def upload_media(request: Request, file: UploadFile = File(...), user: dict = Depends(get_current_user)) -> dict:
    """A picture, video, sound or PDF for a post. Returns the address to place in the text."""
    from app.shared.storage import ContentType, upload_file_detailed

    kinds = {"image": (ContentType.IMAGE, 10), "video": (ContentType.VIDEO, 100), "audio": (ContentType.AUDIO, 25),
             "application/pdf": (ContentType.EXPORT, 20)}
    ctype = (file.content_type or "").lower()
    group = "application/pdf" if ctype == "application/pdf" else ctype.split("/")[0]
    if group not in kinds or ctype in ("image/svg+xml",):
        raise HTTPException(status_code=400, detail="Add a picture (JPG, PNG, WebP, GIF), a video (MP4, WebM), a sound file (MP3, M4A, WAV) or a PDF.")
    target, max_mb = kinds[group]
    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > max_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"That file is too big. The limit is {max_mb} MB.")
    if not data:
        raise HTTPException(status_code=400, detail="That file is empty.")
    out = await upload_file_detailed(bytes(data), target, f"site-content-{user.get('id')}", file.filename)
    return {"url": out["url"], "kind": "file" if group == "application/pdf" else group, "width": out.get("width"),
            "height": out.get("height"), "name": file.filename}


@router.get("/{content_id}")
@limiter.limit("120/minute")
async def item(request: Request, content_id: str) -> dict:
    await C.promote_due()
    return {"item": C.full(await C.get(content_id))}


@router.put("/{content_id}/draft")
@limiter.limit("240/minute")
async def save(request: Request, content_id: str, body: DraftBody, user: dict = Depends(get_current_user)) -> dict:
    return {"item": C.full(await C.save_draft(content_id, body.data, body.base_version, user))}


@router.post("/{content_id}/review")
@limiter.limit("30/minute")
async def review(request: Request, content_id: str, body: NoteBody, user: dict = Depends(get_current_user)) -> dict:
    return {"item": C.full(await C.send_for_review(content_id, body.note, user))}


@router.post("/{content_id}/send-back")
@limiter.limit("30/minute")
async def send_back(request: Request, content_id: str, body: NoteBody, user: dict = Depends(require_action("content", "approve"))) -> dict:
    return {"item": C.full(await C.send_back(content_id, body.note, user))}


@router.post("/{content_id}/publish")
@limiter.limit("30/minute")
async def publish(request: Request, content_id: str, body: PublishBody, user: dict = Depends(require_action("content", "approve"))) -> dict:
    doc = await C.get(content_id)
    await _legal_check(doc, user)
    at = body.at.astimezone(timezone.utc) if body.at and body.at.tzinfo else (body.at.replace(tzinfo=timezone.utc) if body.at else None)
    out = await C.publish(content_id, at, body.note, user)
    await ST.audit(user, "content.scheduled" if out.get("schedule_at") else "content.published", content_id,
                   after={"kind": doc["kind"], "key": doc["key"], "at": at.isoformat() if at else None}, reason=body.note)
    return {"item": C.full(out)}


@router.post("/{content_id}/cancel-schedule")
@limiter.limit("30/minute")
async def cancel_schedule(request: Request, content_id: str, user: dict = Depends(require_action("content", "approve"))) -> dict:
    return {"item": C.full(await C.cancel_schedule(content_id))}


@router.post("/{content_id}/unpublish")
@limiter.limit("30/minute")
async def unpublish(request: Request, content_id: str, body: NoteBody, user: dict = Depends(require_action("content", "approve"))) -> dict:
    out = await C.unpublish(content_id)
    await ST.audit(user, "content.unpublished", content_id, after={"kind": out["kind"], "key": out["key"]}, reason=body.note)
    return {"item": C.full(out)}


@router.delete("/{content_id}")
@limiter.limit("30/minute")
async def delete(request: Request, content_id: str, user: dict = Depends(require_action("content", "approve"))) -> dict:
    await C.remove(content_id)
    await ST.audit(user, "content.deleted", content_id)
    return {"ok": True}


@router.get("/{content_id}/versions")
@limiter.limit("60/minute")
async def versions(request: Request, content_id: str) -> dict:
    return {"versions": await C.versions(content_id)}


@router.get("/{content_id}/versions/{version}")
@limiter.limit("60/minute")
async def version(request: Request, content_id: str, version: int) -> dict:
    await C.get(content_id)
    row = await C.versions_col().find_one({"content_id": content_id, "version": version}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="That version doesn't exist.")
    return {"version": {**row, "by": (row.get("by") or {}).get("name")}}


@router.post("/{content_id}/restore")
@limiter.limit("30/minute")
async def restore(request: Request, content_id: str, body: RestoreBody, user: dict = Depends(get_current_user)) -> dict:
    out = await C.restore(content_id, body.version, user)
    await ST.audit(user, "content.restored", content_id, after={"version": body.version})
    return {"item": C.full(out)}
