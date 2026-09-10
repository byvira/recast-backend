"""Workspace creation and read routes."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.rbac import require_permission
from app.core.tiers import TIER_DEFAULTS
from app.db.mongo import users, workspaces, workspace_members
from app.models.workspace import CreateWorkspaceBody, WorkspaceRole

router = APIRouter()


class UpdateWorkspaceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class SetRoleBody(BaseModel):
    role: WorkspaceRole


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


@router.get("/")
@limiter.limit("100/minute")
async def list_my_workspaces(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List every workspace the caller is an active member of."""
    memberships = await workspace_members.find(
        {"user_id": current_user["id"], "status": "active"}
    ).to_list(length=200)
    ws_ids = [m["workspace_id"] for m in memberships]
    ws_docs = {
        w["id"]: w
        for w in await workspaces.find({"id": {"$in": ws_ids}}).to_list(length=200)
    }
    default_id = current_user.get("default_workspace_id")
    items = []
    for m in memberships:
        w = ws_docs.get(m["workspace_id"])
        if not w:
            continue
        items.append({
            "workspace_id": w["id"],
            "name": w["name"],
            "tier": w.get("tier"),
            "is_personal": w.get("is_personal", False),
            "role": m["role"],
            "is_default": w["id"] == default_id,
        })
    return {"items": items}


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


@router.patch("/{workspace_id}")
@limiter.limit("20/minute")
async def update_workspace(
    request: Request,
    workspace_id: str,
    body: UpdateWorkspaceBody,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Update workspace settings — requires manage_workspace_settings."""
    await require_permission(workspace_id, current_user["id"], "manage_workspace_settings")
    await workspaces.update_one(
        {"id": workspace_id},
        {"$set": {"name": body.name, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"workspace_id": workspace_id, "name": body.name}


@router.delete("/{workspace_id}")
@limiter.limit("10/minute")
async def delete_workspace(
    request: Request,
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a workspace — owner only, and personal workspaces cannot be deleted."""
    ws = await workspaces.find_one({"id": workspace_id})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if ws.get("owner_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the owner can delete a workspace.")
    if ws.get("is_personal"):
        raise HTTPException(status_code=400, detail="Personal workspaces cannot be deleted.")

    await workspaces.delete_one({"id": workspace_id})
    await workspace_members.delete_many({"workspace_id": workspace_id})
    # Reset default_workspace_id for any member who had this as their default
    await users.update_many(
        {"default_workspace_id": workspace_id},
        {"$set": {"default_workspace_id": None}},
    )
    return {"workspace_id": workspace_id, "deleted": True}


@router.put("/{workspace_id}/members/{member_user_id}/role")
@limiter.limit("20/minute")
async def set_member_role(
    request: Request,
    workspace_id: str,
    member_user_id: str,
    body: SetRoleBody,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Change a member's role — requires manage_roles."""
    caller = await require_permission(workspace_id, current_user["id"], "manage_roles")

    ws = await workspaces.find_one({"id": workspace_id})
    if ws and ws.get("owner_id") == member_user_id and body.role != WorkspaceRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot change the owner's role.")

    target = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": member_user_id}, {"role": 1}
    )
    res = await workspace_members.update_one(
        {"workspace_id": workspace_id, "user_id": member_user_id},
        {"$set": {"role": body.role.value}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Member not found.")

    from app.shared.governance_events import emit_role_changed
    emit_role_changed(
        workspace_id, actor_user_id=current_user["id"], actor_role=caller.get("role", ""),
        subject_user_id=member_user_id, from_role=(target or {}).get("role", ""),
        to_role=body.role.value,
    )
    return {"workspace_id": workspace_id, "user_id": member_user_id, "role": body.role.value}


@router.delete("/{workspace_id}/members/{member_user_id}")
@limiter.limit("20/minute")
async def remove_member(
    request: Request,
    workspace_id: str,
    member_user_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Remove a member from a workspace — requires remove_members."""
    caller = await require_permission(workspace_id, current_user["id"], "remove_members")

    ws = await workspaces.find_one({"id": workspace_id})
    if ws and ws.get("owner_id") == member_user_id:
        raise HTTPException(status_code=400, detail="Cannot remove the workspace owner.")

    target = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": member_user_id}, {"role": 1}
    )
    res = await workspace_members.delete_one(
        {"workspace_id": workspace_id, "user_id": member_user_id}
    )
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Member not found.")

    from app.shared.governance_events import emit_member_removed
    emit_member_removed(
        workspace_id, actor_user_id=current_user["id"], actor_role=caller.get("role", ""),
        subject_user_id=member_user_id, role=(target or {}).get("role", ""),
    )
    await users.update_one(
        {"id": member_user_id, "default_workspace_id": workspace_id},
        {"$set": {"default_workspace_id": None}},
    )
    return {"workspace_id": workspace_id, "user_id": member_user_id, "removed": True}