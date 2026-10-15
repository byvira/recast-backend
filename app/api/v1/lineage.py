"""Where an item came from. One read route for every content type: the chain of items it was made from, nearest first, each with
a plain title and its own, AI or stock label, so a screen can say "this video was made from this post, this voice and this
picture" without knowing which pipeline made which."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.agents.chain import lineage
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.db.mongo import get_db

router = APIRouter()

_TITLE_FIELDS = ("title", "name", "filename", "topic", "content")


def _title(doc: dict[str, Any]) -> str:
    for field in _TITLE_FIELDS:
        value = doc.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:80]
    return "Untitled"


async def _describe(workspace_id: str, kind: str, item_id: str) -> dict[str, Any]:
    from app.video.origin import origin_of

    doc = await get_db()[lineage.COLLECTIONS[kind]].find_one({"id": item_id, "workspace_id": workspace_id}) or {}
    origin = None
    if doc:
        try:
            origin = origin_of(doc) if kind in ("image", "media") else ("ai" if kind == "audio" and doc.get("source") == "synthesized" else None)
        except Exception:  # noqa: BLE001 - a label that cannot be worked out is left off
            origin = None
    return {"kind": kind, "id": item_id, "title": _title(doc) if doc else "No longer available", "exists": bool(doc), "origin": origin}


@router.get("/{kind}/{item_id}")
@limiter.limit("120/minute")
async def get_lineage(request: Request, kind: str, item_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    if kind not in lineage.COLLECTIONS:
        raise HTTPException(status_code=404, detail="That kind of item has no history.")
    found = await lineage.ancestors(kind, item_id, ctx.workspace_id)
    items = [{**await _describe(ctx.workspace_id, a["kind"], a["id"]), "child": a["child"]} for a in found]
    return {"kind": kind, "id": item_id, "made_from": items}
