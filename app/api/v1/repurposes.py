"""The record of what was made from what. When a recording, picture or post is repurposed into other content, each result is
noted against its source, so the source can show what came from it and a result can be traced back."""
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import audio_assets, content_pieces, image_assets, repurposes

router = APIRouter()

Kind = Literal["text", "audio", "image", "video"]


class RepurposeOutput(BaseModel):
    kind: Kind
    id: str = Field(min_length=1, max_length=80)
    label: Optional[str] = Field(None, max_length=120)


class RecordRepurposeBody(BaseModel):
    source_kind: Kind
    source_id: str = Field(min_length=1, max_length=80)
    outputs: list[RepurposeOutput] = Field(min_length=1, max_length=20)


def _first_line(text: str, limit: int = 80) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rsplit(" ", 1)[0] + "…"


async def _describe(row: dict, workspace_id: str) -> dict:
    """What the result is called and whether it still exists, read fresh so a rename or a deletion shows."""
    kind, out_id = row["output_kind"], row["output_id"]
    title, status, href, detail = row.get("label") or "", "ready", None, ""
    if kind == "text":
        doc = await content_pieces.find_one(
            {"piece_id": out_id, "workspace_id": workspace_id},
            {"content": 1, "platform": 1, "deleted": 1, "approval_status": 1, "publish_status": 1},
        )
        if not doc or doc.get("deleted"):
            status = "removed"
        else:
            title = _first_line(doc.get("content", "")) or title
            detail = doc.get("platform") or ""
            status = "published" if doc.get("publish_status") == "published" else (doc.get("approval_status") or "ready")
            href = f"/dashboard/drafts?piece={out_id}"
    else:
        collection = image_assets if kind == "image" else audio_assets
        doc = await collection.find_one({"id": out_id, "workspace_id": workspace_id}, {"title": 1, "deleted": 1, "approval_status": 1})
        if not doc or doc.get("deleted"):
            status = "removed"
        else:
            title = _first_line(doc.get("title", "")) or title
            status = str(doc.get("approval_status") or "ready")
            href = f"/dashboard/pipelines/{kind}?asset={out_id}"
    return {
        "id": row["id"],
        "output_kind": kind,
        "output_id": out_id,
        "title": title or "Untitled",
        "detail": detail,
        "status": status,
        "href": href,
        "created_at": row["created_at"].isoformat() if isinstance(row.get("created_at"), datetime) else None,
    }


@router.post("", status_code=201)
@limiter.limit("60/minute")
async def record_repurpose(
    request: Request,
    body: RecordRepurposeBody,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """Note results made from one source. Recording the same result twice keeps one row."""
    now = datetime.now(timezone.utc)
    saved = 0
    for out in body.outputs:
        res = await repurposes.update_one(
            {
                "workspace_id": ctx.workspace_id,
                "source_kind": body.source_kind,
                "source_id": body.source_id,
                "output_kind": out.kind,
                "output_id": out.id,
            },
            {
                "$setOnInsert": {"id": str(uuid4()), "created_at": now, "created_by": ctx.user_id},
                "$set": {"label": out.label},
            },
            upsert=True,
        )
        saved += 1 if res.upserted_id is not None else 0
    return {"recorded": saved}


@router.get("")
@limiter.limit("120/minute")
async def list_repurposes(
    request: Request,
    source_kind: Kind,
    source_id: str = Query(..., min_length=1, max_length=80),
    limit: int = Query(4, ge=1, le=20),
    offset: int = Query(0, ge=0),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """What was made from this source, newest first, a few at a time."""
    flt = {"workspace_id": ctx.workspace_id, "source_kind": source_kind, "source_id": source_id}
    total = await repurposes.count_documents(flt)
    rows = await repurposes.find(flt, {"_id": 0}).sort("created_at", -1).skip(offset).limit(limit).to_list(length=limit)
    return {"items": [await _describe(r, ctx.workspace_id) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.delete("/{record_id}")
@limiter.limit("60/minute")
async def unlink_repurpose(
    request: Request,
    record_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """Take a result off the source's list. The result itself is left as it is."""
    res = await repurposes.delete_one({"id": record_id, "workspace_id": ctx.workspace_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="That item isn't on the list.")
    return {"id": record_id, "unlinked": True}
