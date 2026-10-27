"""Workspace creation and read routes."""

from datetime import timedelta, datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.shared.tier_policy import policy_for
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.core.rbac import require_permission
from app.core.tiers import TIER_DEFAULTS
from app.db.mongo import users, workspaces, workspace_members
from app.models.workspace import CreateWorkspaceBody, MediaUploadLimits, WorkspaceRole
from app.shared import entitlements

#: How long a deleted workspace is kept before everything in it is removed for good.
DELETE_HOLD_DAYS = 30
#: The most workspaces one person can own at a time (their personal one is not counted).
MAX_OWNED_WORKSPACES = 50

router = APIRouter()


class UpdateWorkspaceBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    # Workspace-wide default language — see app.shared.language for the
    # precedence chain that consumes this. Opaque string, no validation
    # against any fixed set; omit the field (not empty string) to leave the
    # current value untouched.
    language: Optional[str] = Field(None, max_length=40, pattern=r"^[A-Za-z][A-Za-z _\-()]*$")
    # Per-kind max upload size (Plan & Quotas) — app.api.v1.media reads
    # this instead of a hardcoded constant. Omit the field to leave the
    # current value (or the hardcoded default, if never set) untouched.
    media_upload_limits: Optional[MediaUploadLimits] = None
    # Whether posts wait for approval. Omit to leave it; null puts it back to what the plan does by default.
    require_review: Optional[bool] = None


class SetRoleBody(BaseModel):
    role: WorkspaceRole


FREE_WORKSPACE_MESSAGE = (
    "A free account has one workspace, and you already have it. Add the new workspace to one of your paid workspaces, "
    "or move a workspace to a paid plan."
)


async def _room_for_new_workspace(user_id: str, billing_group_id: Optional[str]) -> Optional[str]:
    """The paid workspace a new workspace joins, after checking there is room on its plan. While plans are off the link is kept
    for later and nothing more is checked; once they are on, a workspace that joins no paid plan is the owner's one free one."""
    if billing_group_id:
        group = await workspaces.find_one(
            {"id": billing_group_id, "owner_id": user_id, "deleted_at": {"$exists": False}}, {"id": 1, "billing_group_id": 1, "tier_config": 1}
        )
        if not group or group.get("billing_group_id"):
            raise HTTPException(status_code=400, detail="Choose one of your own workspaces that pays for its plan.")
        ent = await entitlements.entitlements_for(billing_group_id)
        await entitlements.require_room(ent, "owned_workspaces", len(await entitlements.group_ids(billing_group_id)))
        return billing_group_id
    if settings.BILLING_ENFORCED:
        standalone_free = 0
        async for w in workspaces.find(
            {"owner_id": user_id, "deleted_at": {"$exists": False}, "billing_group_id": {"$exists": False}}, {"id": 1}
        ):
            if (await entitlements.entitlements_for(w["id"])).plan == "free":
                standalone_free += 1
        if standalone_free >= 1:
            raise HTTPException(status_code=402, detail={"code": "limit_reached", "limit": "owned_workspaces", "allowed": 1,
                                                         "used": standalone_free, "plan": "free", "enforced": True,
                                                         "message": FREE_WORKSPACE_MESSAGE})
    return None


@router.post("/", status_code=201)
@limiter.limit("10/minute")
async def create_workspace(
    request: Request,
    body: CreateWorkspaceBody,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a workspace and add the caller as its owner, in one operation."""
    # Until plans are billed, the number of workspaces one person can own is what limits this: without it a person could make any
    # number of workspaces on the largest plan.
    owned = await workspaces.count_documents(
        {"owner_id": current_user["id"], "is_personal": False, "deleted_at": {"$exists": False}}
    )
    if owned >= MAX_OWNED_WORKSPACES:
        raise HTTPException(status_code=400, detail=f"You already own {MAX_OWNED_WORKSPACES} workspaces, which is the most one person can have.")
    group_id = await _room_for_new_workspace(current_user["id"], body.billing_group_id)
    now = datetime.now(timezone.utc)
    workspace_id = str(uuid4())
    tier_config = TIER_DEFAULTS[body.tier]

    workspace_doc = {
        "id": workspace_id,
        "name": body.name,
        "tier": body.tier.value,
        "tier_config": tier_config.model_dump(),
        "owner_id": current_user["id"],
        "is_personal": False,
        "language": None,
        **({"billing_group_id": group_id} if group_id else {}),
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
    entitlements.forget(group_id)
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
        for w in await workspaces.find({"id": {"$in": ws_ids}, "deleted_at": {"$exists": False}}).to_list(length=200)
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
            "policy": policy_for(w),
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
    if not doc or doc.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Workspace not found.")

    member = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": current_user["id"]}
    )
    if not member:
        raise HTTPException(status_code=403, detail="Access denied.")

    doc.pop("_id", None)
    doc["policy"] = policy_for(doc)
    return doc


@router.get("/{workspace_id}/entitlements")
@limiter.limit("60/minute")
async def get_entitlements(
    request: Request,
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """The workspace's plan, every limit, and what is used of each. Any member may see it."""
    doc = await workspaces.find_one({"id": workspace_id}, {"_id": 0, "id": 1, "deleted_at": 1})
    if not doc or doc.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if not await workspace_members.find_one({"workspace_id": workspace_id, "user_id": current_user["id"], "status": "active"}):
        raise HTTPException(status_code=403, detail="Access denied.")
    return await entitlements.summary(await entitlements.entitlements_for(workspace_id))


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
    user_ids = [d["user_id"] for d in docs if d.get("user_id")]
    user_docs = {
        u["id"]: u
        for u in await users.find(
            {"id": {"$in": user_ids}}, {"id": 1, "name": 1, "email": 1, "username": 1}
        ).to_list(length=100)
    }
    for d in docs:
        d.pop("_id", None)
        u = user_docs.get(d["user_id"], {})
        d["name"] = u.get("name", "")
        d["email"] = u.get("email", "")
        d["username"] = u.get("username", "")
    return {"items": docs}


@router.patch("/{workspace_id}")
@limiter.limit("20/minute")
async def update_workspace(
    request: Request,
    workspace_id: str,
    body: UpdateWorkspaceBody,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Update workspace settings — requires manage_workspace_settings.

    Only fields actually present in the request body are touched (PATCH
    semantics) — a caller setting `language` alone must not blank out `name`,
    and vice versa.
    """
    await require_permission(workspace_id, current_user["id"], "manage_workspace_settings")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")
    updates["updated_at"] = datetime.now(timezone.utc)
    await workspaces.update_one({"id": workspace_id}, {"$set": updates})
    updates.pop("updated_at")
    fresh = await workspaces.find_one({"id": workspace_id}, {"tier": 1, "require_review": 1})
    return {"workspace_id": workspace_id, **updates, "policy": policy_for(fresh)}


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

    members = await workspace_members.find(
        {"workspace_id": workspace_id}, {"user_id": 1}
    ).to_list(length=200)
    member_ids = {m["user_id"] for m in members if m.get("user_id")}
    member_ids.add(current_user["id"])

    # Not removed at once: the workspace is hidden from everyone and kept for DELETE_HOLD_DAYS, so a mistake can be undone (see
    # restore below). After that a daily job removes everything it owned (see app.workers.workspace_purge).
    now = datetime.now(timezone.utc)
    await workspaces.update_one(
        {"id": workspace_id},
        {"$set": {"deleted_at": now, "purge_after": now + timedelta(days=DELETE_HOLD_DAYS), "deleted_by": current_user["id"]}},
    )
    # Everyone who had it as their default is moved to a workspace that still exists.
    from app.core.workspace import usable_workspace_id

    for uid in member_ids:
        await users.update_one(
            {"id": uid, "default_workspace_id": workspace_id},
            {"$set": {"default_workspace_id": await usable_workspace_id(uid)}},
        )

    recipients = await users.find({"id": {"$in": list(member_ids)}}, {"email": 1}).to_list(length=200)
    for r in recipients:
        if r.get("email"):
            await send_templated_email(
                "workspace-deleted",
                r["email"],
                {"WORKSPACE_NAME": ws["name"], "DELETED_BY_NAME": current_user["name"]},
            )

    return {"workspace_id": workspace_id, "deleted": True, "restorable_until": (now + timedelta(days=DELETE_HOLD_DAYS)).isoformat()}


@router.post("/{workspace_id}/restore")
@limiter.limit("10/minute")
async def restore_workspace(
    request: Request,
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Bring back a workspace deleted in the last DELETE_HOLD_DAYS days. Owner only."""
    ws = await workspaces.find_one({"id": workspace_id})
    if not ws or not ws.get("deleted_at"):
        raise HTTPException(status_code=404, detail="There is no deleted workspace with that id.")
    if ws.get("owner_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the owner can bring a workspace back.")
    # A workspace brought back counts again, the same as a new one: the most one person can own, and its plan's room.
    if not ws.get("is_personal"):
        owned = await workspaces.count_documents(
            {"owner_id": current_user["id"], "is_personal": False, "deleted_at": {"$exists": False}}
        )
        if owned >= MAX_OWNED_WORKSPACES:
            raise HTTPException(status_code=400, detail=f"You already own {MAX_OWNED_WORKSPACES} workspaces, which is the most one person can have.")
        if ws.get("billing_group_id"):
            await _room_for_new_workspace(current_user["id"], ws["billing_group_id"])
        elif (ws.get("plan") or "free") == "free":
            await _room_for_new_workspace(current_user["id"], None)
    await workspaces.update_one(
        {"id": workspace_id}, {"$unset": {"deleted_at": "", "purge_after": "", "deleted_by": ""}},
    )
    entitlements.forget(ws.get("billing_group_id") or workspace_id)
    return {"workspace_id": workspace_id, "restored": True}


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
    handing_over = body.role == WorkspaceRole.OWNER and ws and ws.get("owner_id") != member_user_id
    if handing_over:
        # There is one owner. Making someone else the owner is a hand-over: only the current owner can do it, the workspace's owner
        # becomes that person, and the previous owner stays on as an admin. (Two people holding the owner role while only one was the
        # owner on record left deleting the workspace and changing the plan in a half-way state.)
        if ws.get("owner_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Only the current owner can hand the workspace to someone else.")
        if not target:
            raise HTTPException(status_code=404, detail="Member not found.")
        # Once plans are on, a workspace that does not pay for its own plan becomes the new owner's free workspace: it leaves the
        # previous owner's plan, and the new owner must not already have a free one. A paid workspace keeps its plan.
        leaves_group = False
        if settings.BILLING_ENFORCED and (ws.get("billing_group_id") or (ws.get("plan") or "free") == "free"):
            async for w in workspaces.find(
                {"owner_id": member_user_id, "deleted_at": {"$exists": False}, "billing_group_id": {"$exists": False}}, {"id": 1}
            ):
                if (await entitlements.entitlements_for(w["id"])).plan == "free":
                    raise HTTPException(status_code=402, detail={
                        "code": "limit_reached", "limit": "owned_workspaces", "allowed": 1, "used": 1, "plan": "free", "enforced": True,
                        "message": "They already have a free workspace, so this one cannot be handed to them as another free one. "
                                   "Put this workspace on a paid plan first, or hand it to someone without a free workspace.",
                    })
            leaves_group = bool(ws.get("billing_group_id"))
        await workspaces.update_one(
            {"id": workspace_id}, {"$set": {"owner_id": member_user_id}, **({"$unset": {"billing_group_id": ""}} if leaves_group else {})},
        )
        if leaves_group:
            entitlements.forget(ws["billing_group_id"])
        entitlements.forget(workspace_id)
        await workspace_members.update_one(
            {"workspace_id": workspace_id, "user_id": current_user["id"]}, {"$set": {"role": WorkspaceRole.ADMIN.value}},
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

    subject = await users.find_one({"id": member_user_id}, {"email": 1})
    if subject and subject.get("email"):
        await send_templated_email(
            "role-changed",
            subject["email"],
            {
                "WORKSPACE_NAME": (ws or {}).get("name", "your workspace"),
                "OLD_ROLE": (target or {}).get("role", ""),
                "NEW_ROLE": body.role.value,
                "CHANGED_BY_NAME": current_user["name"],
            },
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

    subject = await users.find_one({"id": member_user_id}, {"email": 1})
    if subject and subject.get("email"):
        await send_templated_email(
            "member-removed",
            subject["email"],
            {
                "WORKSPACE_NAME": (ws or {}).get("name", "your workspace"),
                "REMOVED_BY_NAME": current_user["name"],
            },
        )

    from app.core.workspace import usable_workspace_id

    await users.update_one(
        {"id": member_user_id, "default_workspace_id": workspace_id},
        {"$set": {"default_workspace_id": await usable_workspace_id(member_user_id)}},
    )
    return {"workspace_id": workspace_id, "user_id": member_user_id, "removed": True}