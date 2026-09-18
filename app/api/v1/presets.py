"""Content preset endpoints — reusable structure templates.

Workspace-scoped. Reads require membership; create/edit/delete require
``create_content``/``edit_content`` (same gate content_pieces mutations
use — a preset is a saved shape for content, not a distinct resource
needing its own permission tier).
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import presets
from app.models.preset import CreatePresetRequest, Preset, UpdatePresetRequest

router = APIRouter()
logger = logging.getLogger(__name__)


def _doc_to_preset(doc: dict) -> Preset:
    return Preset(**{k: v for k, v in doc.items() if k != "_id" and k != "deleted"})


@router.post("/", response_model=Preset, status_code=201)
@limiter.limit("30/minute")
async def create_preset(
    request: Request,
    body: CreatePresetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> Preset:
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid4()),
        "workspace_id": ctx.workspace_id,
        "brand_id": None,
        "version": 1,
        **body.model_dump(),
        "usage_count": 0,
        "tone_score": 0,
        "is_system_default": False,
        "created_by": ctx.user_id,
        "created_at": now,
        "updated_at": now,
        "deleted": False,
    }
    await presets.insert_one(doc)
    return _doc_to_preset(doc)


@router.get("/", response_model=list[Preset])
@limiter.limit("60/minute")
async def list_presets(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> list[Preset]:
    docs = await presets.find(
        {"workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
        sort=[("updated_at", -1)],
    ).to_list(length=200)
    return [_doc_to_preset(d) for d in docs]


@router.get("/{preset_id}", response_model=Preset)
@limiter.limit("60/minute")
async def get_preset(
    request: Request,
    preset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Preset:
    doc = await presets.find_one(
        {"id": preset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Preset not found.")
    return _doc_to_preset(doc)


@router.patch("/{preset_id}", response_model=Preset)
@limiter.limit("30/minute")
async def update_preset(
    request: Request,
    preset_id: str,
    body: UpdatePresetRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> Preset:
    existing = await presets.find_one(
        {"id": preset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Preset not found.")

    update: dict[str, Any] = {
        k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None
    }
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    update["version"] = existing.get("version", 1) + 1
    update["updated_at"] = datetime.now(timezone.utc)

    await presets.update_one(
        {"id": preset_id, "workspace_id": ctx.workspace_id}, {"$set": update}
    )
    updated = await presets.find_one({"id": preset_id, "workspace_id": ctx.workspace_id})
    return _doc_to_preset(updated)


@router.delete("/{preset_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_preset(
    request: Request,
    preset_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> None:
    result = await presets.update_one(
        {"id": preset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
        {"$set": {"deleted": True, "updated_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Preset not found.")
