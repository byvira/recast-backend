"""Workspace creation and read routes."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.tiers import TIER_DEFAULTS
from app.db.mongo import workspaces, workspace_members
from app.models.workspace import CreateWorkspaceBody, WorkspaceRole

router = APIRouter()


@router.post("/", status_code=201)
@limiter.limit("10/minute")
async def create_workspace(
    request: Request,
    body: CreateWorkspaceBody,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a workspace and add the caller as its owner, in one operation."""
    now = datetime.now(timezone.utc)
    workspace_id = str(uuid4())
    tier_config = TIER_DEFAULTS[body.tier]

    workspace_doc = {
        "id": workspace_id,
        "name": body.name,
        "tier": body.tier.value,
        "tier_config": tier_config.model_dump(),
        "owner_id": current_user["id"],
        "created_at": now,
        "updated_at": now,
    }

    member_doc = {
        "id": str(uuid4()),
        "workspace_id": workspace_id,
        "user_id": current_user["id"],
        "role": WorkspaceRole.OWNER.value,
        "status": "active",
        "joined_at": now,
    }

    await workspaces.insert_one(workspace_doc)
    try:
        await workspace_members.insert_one(member_doc)
    except Exception:
        await workspaces.delete_one({"id": workspace_id})
        raise HTTPException(status_code=500, detail="Failed to create workspace.")

    return {
        "workspace_id": workspace_id,
        "tier": body.tier.value,
        "tier_config": tier_config.model_dump(),
    }


@router.get("/{workspace_id}")
@limiter.limit("100/minute")
async def get_workspace(
    request: Request,
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Fetch a workspace — caller must be a member."""
    doc = await workspaces.find_one({"id": workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    member = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": current_user["id"]}
    )
    if not member:
        raise HTTPException(status_code=403, detail="Access denied.")

    doc.pop("_id", None)
    return doc


@router.get("/{workspace_id}/members")
@limiter.limit("100/minute")
async def list_members(
    request: Request,
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List all members of a workspace — caller must be a member."""
    requester = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": current_user["id"]}
    )
    if not requester:
        raise HTTPException(status_code=403, detail="Access denied.")

    docs = await workspace_members.find({"workspace_id": workspace_id}).to_list(length=100)
    for d in docs:
        d.pop("_id", None)
    return {"items": docs}