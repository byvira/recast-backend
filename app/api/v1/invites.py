"""Invite creation, listing, preview, and acceptance routes."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.core.rbac import require_permission
from app.db.mongo import invites, users, workspace_members, workspaces
from app.models.workspace import InviteMemberBody, InviteStatus

router = APIRouter()


def _is_expired(expires_at) -> bool:
    """Compare an invite expiry (possibly tz-naive when read back from Mongo) against now (UTC)."""
    if expires_at is None:
        return True
    if getattr(expires_at, "tzinfo", None) is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


def _effective_status(invite: dict) -> str:
    """The invite's displayable status.

    Nothing proactively flips an invite's stored status to "expired" once
    its expires_at passes — it just sits there still saying "pending"
    until someone tries to preview/accept it (which 410s without writing
    anything back). Callers that display status (list, history) need the
    real picture rather than a stale "pending" forever.
    """
    if invite["status"] == InviteStatus.PENDING.value and _is_expired(invite["expires_at"]):
        return InviteStatus.EXPIRED.value
    return invite["status"]


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

    invite_link = f"{settings.FRONTEND_URL}/invite/{invite_doc['token']}"
    email_sent = await send_templated_email(
        "workspace-invite",
        invite_doc["email"],
        {
            "INVITER_NAME": current_user["name"],
            "WORKSPACE_NAME": ws["name"],
            "ROLE": invite_doc["role"],
            "INVITE_LINK": invite_link,
            "EXPIRES_IN": "7 days",
            "INVITER_INITIAL": current_user["name"][:1].upper() if current_user.get("name") else "?",
        },
    )

    return {
        "invite_id": invite_doc["id"],
        "token": invite_doc["token"],
        # send_templated_email swallows delivery failures (logs + returns
        # False) so create_invite never 500s over an email provider hiccup —
        # but the caller still needs to know, since the invite exists either
        # way and the only real fallback is sharing the link directly.
        "email_sent": email_sent,
    }


@router.post("/{workspace_id}/{invite_id}/resend")
@limiter.limit("20/minute")
async def resend_invite(
    request: Request,
    workspace_id: str,
    invite_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Resend a pending (or chronologically expired) invite.

    Reuses the same token/link rather than minting a new one — if the
    recipient still has the original email open, that link keeps working.
    Refreshes expires_at to another 7 days out. Requires invite_members,
    same as sending one.
    """
    await require_permission(workspace_id, current_user["id"], "invite_members")

    invite = await invites.find_one({"id": invite_id, "workspace_id": workspace_id})
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found.")
    if invite["status"] not in (InviteStatus.PENDING.value, InviteStatus.EXPIRED.value):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resend an invite that is already {invite['status']}.",
        )

    ws = await workspaces.find_one({"id": workspace_id})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    new_expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    await invites.update_one(
        {"id": invite_id},
        {"$set": {"status": InviteStatus.PENDING.value, "expires_at": new_expires_at}},
    )

    invite_link = f"{settings.FRONTEND_URL}/invite/{invite['token']}"
    email_sent = await send_templated_email(
        "workspace-invite",
        invite["email"],
        {
            "INVITER_NAME": current_user["name"],
            "WORKSPACE_NAME": ws["name"],
            "ROLE": invite["role"],
            "INVITE_LINK": invite_link,
            "EXPIRES_IN": "7 days",
            "INVITER_INITIAL": current_user["name"][:1].upper() if current_user.get("name") else "?",
        },
    )

    return {
        "invite_id": invite_id,
        "workspace_id": workspace_id,
        "expires_at": new_expires_at.isoformat(),
        "email_sent": email_sent,
    }


@router.get("/{workspace_id}")
@limiter.limit("50/minute")
async def list_invites(
    request: Request,
    workspace_id: str,
    status: str = Query("pending", pattern="^(pending|all)$"),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List invites for a workspace.

    status="pending" (default): only invites still actually awaiting a
    response — chronologically expired ones are excluded even though their
    stored status still says "pending" (see _effective_status).
    status="all": full history including accepted/expired/revoked, newest
    first, each with its real effective status.
    """
    await require_permission(workspace_id, current_user["id"], "invite_members")

    query: dict[str, Any] = {"workspace_id": workspace_id}
    if status == "pending":
        query["status"] = InviteStatus.PENDING.value

    docs = await invites.find(query).sort("created_at", -1).to_list(length=200)
    for d in docs:
        d.pop("_id", None)
        d["status"] = _effective_status(d)

    if status == "pending":
        docs = [d for d in docs if d["status"] == InviteStatus.PENDING.value]

    return {"items": docs}


@router.delete("/{workspace_id}/{invite_id}")
@limiter.limit("20/minute")
async def revoke_invite(
    request: Request,
    workspace_id: str,
    invite_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Revoke a pending invite — requires invite_members (same as sending one)."""
    await require_permission(workspace_id, current_user["id"], "invite_members")

    invite = await invites.find_one({"id": invite_id, "workspace_id": workspace_id})
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found.")
    if invite["status"] != InviteStatus.PENDING.value:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot revoke an invite that is already {invite['status']}.",
        )

    await invites.update_one(
        {"id": invite_id}, {"$set": {"status": InviteStatus.REVOKED.value}}
    )

    return {"invite_id": invite_id, "workspace_id": workspace_id, "revoked": True}


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
    """Accept an invite — requires the user to be logged in as the invited
    identity. Rejects with 403 if the caller's account isn't the one the
    invite was actually sent to (previously any authenticated holder of the
    token could accept as themselves, regardless of email)."""
    invite = await invites.find_one({"token": token})
    if not invite or invite["status"] != InviteStatus.PENDING.value:
        raise HTTPException(status_code=404, detail="Invite not found or no longer valid.")
    if _is_expired(invite["expires_at"]):
        raise HTTPException(status_code=410, detail="Invite has expired.")

    invited_email = invite["email"].lower()
    caller_identifiers = {current_user.get("email", "").lower()}
    caller_identifiers.update(i.lower() for i in current_user.get("auth_identifiers", []))
    caller_identifiers.discard("")
    if invited_email not in caller_identifiers:
        raise HTTPException(
            status_code=403,
            detail=f"This invite was sent to {invite['email']}. Log in with that email to accept it.",
        )

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

    inviter = await users.find_one({"id": invite["invited_by"]}, {"email": 1})
    if inviter and inviter.get("email"):
        ws = await workspaces.find_one({"id": invite["workspace_id"]}, {"name": 1})
        await send_templated_email(
            "invite-accepted",
            inviter["email"],
            {
                "NEW_MEMBER_NAME": current_user["name"],
                "WORKSPACE_NAME": (ws or {}).get("name", "your workspace"),
                "ROLE": invite["role"],
            },
        )

    return {"workspace_id": invite["workspace_id"], "role": invite["role"]}