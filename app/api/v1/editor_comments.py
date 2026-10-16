"""Comments on a piece of work inside the workspace: a post, a recording, a picture or a video project. One small model for all four editors,
so a note, a reply and "mark as done" behave the same everywhere. (A video also has review links for people outside the workspace;
this is for the team.)

Anyone in the workspace may read and write comments, because a reviewer who cannot edit is exactly who comments. Marking a comment
done and deleting one are for members who can edit content, and a person may always delete their own."""

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.rbac import assert_permission
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import audio_assets, content_pieces, get_db, image_assets

logger = logging.getLogger(__name__)
router = APIRouter()

TargetType = Literal["piece", "audio", "image", "video"]
MAX_PER_TARGET = 300
MAX_REPLIES = 100


def _comments():
    return get_db()["editor_comments"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    if not isinstance(v, datetime):
        return None
    v = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return v.isoformat().replace("+00:00", "Z")


async def _target_exists(target_type: str, target_id: str, workspace_id: str) -> bool:
    if target_type == "piece":
        return bool(await content_pieces.find_one({"piece_id": target_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}, {"_id": 1}))
    if target_type == "audio":
        return bool(await audio_assets.find_one({"id": target_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}, {"_id": 1}))
    if target_type == "video":
        return bool(await get_db()["video_projects"].find_one({"id": target_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}, {"_id": 1}))
    return bool(await image_assets.find_one({"id": target_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}, {"_id": 1}))


def _view(c: dict) -> dict[str, Any]:
    return {
        "id": c["id"], "target_type": c["target_type"], "target_id": c["target_id"], "text": c["text"], "author_id": c["author_id"],
        "author_name": c["author_name"], "anchor": c.get("anchor"), "created_at": _iso(c.get("created_at")), "resolved": bool(c.get("resolved")),
        "resolved_by": c.get("resolved_by_name"), "replies": [
            {"id": r["id"], "text": r["text"], "author_id": r.get("author_id"), "author_name": r["author_name"], "created_at": _iso(r.get("created_at"))}
            for r in c.get("replies", [])
        ],
    }


class NewComment(BaseModel):
    target_type: TargetType
    target_id: str = Field(..., min_length=1, max_length=80)
    text: str = Field(..., min_length=1, max_length=2000)
    #: where in the work it points: a block id for a post, a time in milliseconds for audio, a slide number for a picture
    anchor: Optional[dict[str, Any]] = None


class ReplyBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class ResolveBody(BaseModel):
    resolved: bool = True


def _clean_anchor(raw: Optional[dict]) -> Optional[dict]:
    if not raw:
        return None
    out: dict[str, Any] = {}
    for key, kind in (("block_id", str), ("time_ms", int), ("slide", int)):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, kind):
            continue
        if kind is str and not 1 <= len(value) <= 40:
            continue
        if kind is int and not 0 <= value <= 10**9:
            continue
        out[key] = value
    return out or None


@router.get("")
@limiter.limit("120/minute")
async def list_comments(
    request: Request, target_type: TargetType = Query(...), target_id: str = Query(..., max_length=80),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    if not await _target_exists(target_type, target_id, ctx.workspace_id):
        raise HTTPException(status_code=404, detail="That work was not found.")
    rows = await _comments().find({"workspace_id": ctx.workspace_id, "target_type": target_type, "target_id": target_id}).sort("created_at", 1).to_list(MAX_PER_TARGET)
    views = [_view(r) for r in rows]
    return {"comments": views, "open": sum(1 for v in views if not v["resolved"])}


@router.post("", status_code=201)
@limiter.limit("60/minute")
async def add_comment(request: Request, body: NewComment, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    if not await _target_exists(body.target_type, body.target_id, ctx.workspace_id):
        raise HTTPException(status_code=404, detail="That work was not found.")
    if await _comments().count_documents({"workspace_id": ctx.workspace_id, "target_type": body.target_type, "target_id": body.target_id}) >= MAX_PER_TARGET:
        raise HTTPException(status_code=409, detail="This piece has as many notes as it can hold. Mark some as done and delete them first.")
    now = _now()
    doc = {
        "id": uuid4().hex, "workspace_id": ctx.workspace_id, "target_type": body.target_type, "target_id": body.target_id, "text": body.text.strip(),
        "author_id": ctx.user_id, "author_name": ctx.user.get("name") or ctx.user.get("username") or "A teammate", "anchor": _clean_anchor(body.anchor),
        "created_at": now, "resolved": False, "replies": [],
    }
    if not doc["text"]:
        raise HTTPException(status_code=400, detail="Write a note first.")
    await _comments().insert_one(dict(doc))
    return _view(doc)


@router.post("/{comment_id}/replies", status_code=201)
@limiter.limit("60/minute")
async def reply(request: Request, comment_id: str, body: ReplyBody, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write a reply first.")
    row = await _comments().find_one({"id": comment_id, "workspace_id": ctx.workspace_id})
    if not row:
        raise HTTPException(status_code=404, detail="That note was not found.")
    if len(row.get("replies", [])) >= MAX_REPLIES:
        raise HTTPException(status_code=409, detail="This note has as many replies as it can hold.")
    item = {"id": uuid4().hex, "text": text, "author_id": ctx.user_id, "author_name": ctx.user.get("name") or ctx.user.get("username") or "A teammate", "created_at": _now()}
    await _comments().update_one({"id": comment_id, "workspace_id": ctx.workspace_id}, {"$push": {"replies": item}})
    fresh = await _comments().find_one({"id": comment_id, "workspace_id": ctx.workspace_id})
    return _view(fresh)


@router.put("/{comment_id}/resolved")
@limiter.limit("60/minute")
async def resolve(request: Request, comment_id: str, body: ResolveBody, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    update: dict[str, Any] = {"resolved": body.resolved}
    if body.resolved:
        update.update({"resolved_by": ctx.user_id, "resolved_by_name": ctx.user.get("name") or ctx.user.get("username") or "A teammate", "resolved_at": _now()})
    res = await _comments().find_one_and_update({"id": comment_id, "workspace_id": ctx.workspace_id}, {"$set": update}, return_document=True)
    if not res:
        raise HTTPException(status_code=404, detail="That note was not found.")
    return _view(res)


@router.delete("/{comment_id}", status_code=204)
@limiter.limit("60/minute")
async def delete(request: Request, comment_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> Response:
    row = await _comments().find_one({"id": comment_id, "workspace_id": ctx.workspace_id})
    if not row:
        raise HTTPException(status_code=404, detail="That note was not found.")
    if row.get("author_id") != ctx.user_id:
        # only members who can edit content may delete someone else's note
        try:
            assert_permission(ctx.member, "edit_content")
        except HTTPException:
            raise HTTPException(status_code=403, detail="You can delete your own notes. Ask an editor to remove someone else's.")
    await _comments().delete_one({"id": comment_id, "workspace_id": ctx.workspace_id})
    return Response(status_code=204)
