"""A person's settings for the channels that bring work to them: the digest email, and the workspace's email-in address."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.middleware import limiter
from app.core.rbac import assert_permission
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.tools import digest, email_in

router = APIRouter()


class Toggle(BaseModel):
    enabled: bool


@router.get("/digest")
@limiter.limit("60/minute")
async def get_digest(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return {"enabled": await digest.is_enabled(ctx.workspace_id, ctx.user_id)}


@router.put("/digest")
@limiter.limit("30/minute")
async def put_digest(request: Request, body: Toggle, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """Turn the morning digest on or off for yourself. It only lists what you may approve."""
    await digest.set_enabled(ctx.workspace_id, ctx.user_id, body.enabled)
    return {"enabled": body.enabled}


@router.get("/email-in")
@limiter.limit("60/minute")
async def get_email_in(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    assert_permission(ctx.member, "manage_workspace_settings")
    return {"has_address": await email_in.exists(ctx.workspace_id)}


@router.post("/email-in/new-address", status_code=201)
@limiter.limit("10/minute")
async def new_email_in_address(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """Make the workspace's private address, or a new one. The old one stops working at once. The address is shown now and never kept in
    a form that can be read back."""
    assert_permission(ctx.member, "manage_workspace_settings")
    address = await email_in.rotate(ctx.workspace_id)
    if not address:
        raise HTTPException(status_code=503, detail="Email in is not set up yet.")
    return {"address": address, "note": "Forward a link or some text to this address and it becomes a draft. Only people in this workspace can use it."}
