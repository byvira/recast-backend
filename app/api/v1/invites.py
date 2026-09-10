"""Invite creation, listing, preview, and acceptance routes."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.rbac import require_permission
from app.db.mongo import invites, workspace_members, workspaces
from app.models.workspace import InviteMemberBody, InviteStatus

router = APIRouter()


def _is_expired(expires_at) -> bool:
    """Compare an invite expiry (possibly tz-naive when read back from Mongo) against now (UTC)."""
    if expires_at is None:
        return True
    if getattr(expires_at, "tzinfo", None) is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


@router.post("/{workspace_id}", status_code=201)
@limiter.limit("20/minute")
async def create_invite(
    request: Request,
    workspace_id: str,
    body: InviteMemberBody,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a pending invite, enforcing the workspace's tier seat limit."""
    await require_permission(workspace_id, current_user["id"], "invite_members")

    ws = await workspaces.find_one({"id": workspace_id})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    active_count = await workspace_members.count_documents(
        {"workspace_id": workspace_id, "status": "active"}
    )
    pending_count = await invites.count_documents(
        {"workspace_id": workspace_id, "status": InviteStatus.PENDING.value}
    )
    seats = ws["tier_config"]["seats"]
    if active_count + pending_count >= seats:
        raise HTTPException(status_code=400, detail="Seat limit reached for this tier.")

    now = datetime.now(timezone.utc)
    invite_doc = {
        "id": str(uuid4()),
        "workspace_id": workspace_id,
        "email": body.email.lower(),
        "role": body.role.value,
        "token": secrets.token_urlsafe(32),
        "status": InviteStatus.PENDING.value,
        "invited_by": current_user["id"],
        "created_at": now,
        "expires_at": now + timedelta(days=7),
    }
    await invites.insert_one(invite_doc)

    # TODO: send email with invite link containing invite_doc["token"]

    return {"invite_id": invite_doc["id"], "token": invite_doc["token"]}


@router.get("/{workspace_id}")
@limiter.limit("50/minute")
async def list_invites(
    request: Request,
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List pending invites for a workspace."""
    await require_permission(workspace_id, current_user["id"], "invite_members")
    docs = await invites.find(
        {"workspace_id": workspace_id, "status": InviteStatus.PENDING.value}
    ).to_list(length=100)
    for d in docs:
        d.pop("_id", None)
    return {"items": docs}


@router.get("/accept/{token}")
@limiter.limit("30/minute")
async def preview_invite(request: Request, token: str) -> dict[str, Any]:
    """Preview an invite before acceptance — no auth required."""
    invite = await invites.find_one({"token": token})
    if not invite or invite["status"] != InviteStatus.PENDING.value:
        raise HTTPException(status_code=404, detail="Invite not found or no longer valid.")
    if _is_expired(invite["expires_at"]):
        raise HTTPException(status_code=410, detail="Invite has expired.")

    ws = await workspaces.find_one({"id": invite["workspace_id"]})
    return {
        "workspace_name": ws["name"] if ws else "",
        "role": invite["role"],
        "email": invite["email"],
    }


@router.post("/accept/{token}")
@limiter.limit("10/minute")
async def accept_invite(
    request: Request,
    token: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Accept an invite — requires the user to be logged in."""
    invite = await invites.find_one({"token": token})
    if not invite or invite["status"] != InviteStatus.PENDING.value:
        raise HTTPException(status_code=404, detail="Invite not found or no longer valid.")
    if _is_expired(invite["expires_at"]):
        raise HTTPException(status_code=410, detail="Invite has expired.")

    existing = await workspace_members.find_one(
        {"workspace_id": invite["workspace_id"], "user_id": current_user["id"]}
    )
    if existing:
        raise HTTPException(status_code=409, detail="Already a member of this workspace.")

    now = datetime.now(timezone.utc)
    await workspace_members.insert_one({
        "id": str(uuid4()),
        "workspace_id": invite["workspace_id"],
        "user_id": current_user["id"],
        "role": invite["role"],
        "status": "active",
        "joined_at": now,
    })
    await invites.update_one(
        {"id": invite["id"]}, {"$set": {"status": InviteStatus.ACCEPTED.value}}
    )

    from app.shared.governance_events import emit_member_added
    emit_member_added(
        invite["workspace_id"], actor_user_id=current_user["id"], actor_role=invite["role"],
        subject_user_id=current_user["id"], role=invite["role"],
    )

    return {"workspace_id": invite["workspace_id"], "role": invite["role"]}