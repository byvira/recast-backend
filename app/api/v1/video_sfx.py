"""The sound effects pack for video: the list, the files, and adding one to a workspace's library. The pack is Ekavo's own work under CC0
and every effect carries its licence record. The files are public so the render worker can fetch them; nothing in them is private."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import media_assets
from app.video import sfx

router = APIRouter()


def _item(e: dict) -> dict[str, Any]:
    return {"id": e["id"], "title": e["title"], "category": e["category"], "duration_s": e["duration_s"], "preview_url": sfx.public_url(settings.API_PUBLIC_URL, e["id"]),
            "licence": e["licence"]["name"], "licence_url": e["licence"]["url"], "author": e["licence"]["author"]}


@router.get("")
@limiter.limit("60/minute")
async def list_effects(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return {"effects": [_item(e) for e in sfx.catalog()]}


@router.get("/files/{name}")
@limiter.limit("600/minute")
async def effect_file(request: Request, name: str) -> FileResponse:
    """The mp3 of one effect. Public and cacheable. Only a name from the catalog is served."""
    effect_id = name[:-4] if name.endswith(".mp3") else ""
    path = sfx.path_of(effect_id)
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="That sound effect was not found.")
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=86400, immutable"})


@router.post("/{effect_id}/add", status_code=201)
@limiter.limit("60/minute")
async def add_effect(request: Request, effect_id: str, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Put an effect in this workspace's library so a clip can use it. Adding the same effect again returns the one already there."""
    effect = sfx.get(effect_id)
    if not effect:
        raise HTTPException(status_code=404, detail="That sound effect was not found.")
    existing = await media_assets.find_one({"workspace_id": ctx.workspace_id, "sfx_id": effect_id})
    if existing:
        return {"media_id": existing["id"], "duration_s": existing.get("duration_s"), "created": False}
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid4()), "workspace_id": ctx.workspace_id, "kind": "audio", "url": sfx.public_url(settings.API_PUBLIC_URL, effect_id), "mime_type": "audio/mpeg",
        "duration_s": effect["duration_s"], "size_bytes": effect["bytes"], "source": "library", "created_by": ctx.user_id, "created_at": now,
        "origin": "own", "contains_ai": False, "contains_stock": False, "sfx_id": effect_id, "title": effect["title"],
        "stock": {"provider": "Ekavo sound effects", "id": effect_id, "licence": effect["licence"]["name"], "licence_url": effect["licence"]["url"],
                  "author": effect["licence"]["author"], "origin": effect["licence"]["origin"]},
    }
    await media_assets.insert_one(dict(doc))
    return {"media_id": doc["id"], "duration_s": doc["duration_s"], "created": True}
