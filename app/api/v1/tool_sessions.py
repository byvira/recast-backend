"""The person's side of a command line or assistant sign-in: check the code, say yes or no, and see and revoke what is signed in.

Everything here is done by a signed-in member in the app. A sign-in is for one workspace the person belongs to, with only what their own role allows."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.rbac import ROLE_PERMISSIONS
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.db.mongo import get_db
from app.tools import sessions

router = APIRouter()


class CodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=4, max_length=20)


class ApproveBody(CodeBody):
    workspace_id: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["read"], max_length=4)
    allow_approve: bool = False


def _fail(exc: sessions.SessionError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


def _can(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role or "", set())


@router.post("/device/lookup")
@limiter.limit("20/minute")
async def lookup(request: Request, body: CodeBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """What is asking to sign in, and the workspaces the person could give it."""
    try:
        found = await sessions.lookup(body.code)
    except sessions.SessionError as exc:
        raise _fail(exc)
    members = await get_db()["workspace_members"].find({"user_id": user["id"], "status": {"$ne": "removed"}}, {"workspace_id": 1, "role": 1}).to_list(length=50)
    names = {w["id"]: w.get("name") for w in await get_db()["workspaces"].find({"id": {"$in": [m["workspace_id"] for m in members]}}, {"id": 1, "name": 1}).to_list(length=50)}
    return {**found, "workspaces": [
        {"id": m["workspace_id"], "name": names.get(m["workspace_id"]) or "Workspace", "role": m.get("role"),
         "can": {"create": _can(m.get("role", ""), "create_content"), "publish": _can(m.get("role", ""), "publish_content"), "approve": _can(m.get("role", ""), "approve_content")}}
        for m in members
    ]}


@router.post("/device/approve")
@limiter.limit("10/minute")
async def approve(request: Request, body: ApproveBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return await sessions.approve(body.code, user["id"], body.workspace_id, body.scopes, allow_approve=body.allow_approve)
    except sessions.SessionError as exc:
        raise _fail(exc)


@router.post("/device/deny")
@limiter.limit("10/minute")
async def deny(request: Request, body: CodeBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        await sessions.deny(body.code)
    except sessions.SessionError as exc:
        raise _fail(exc)
    return {"ok": True}


@router.get("")
@limiter.limit("60/minute")
async def list_sessions(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """The workspace's command line and assistant sign-ins. A person sees their own; an owner or admin sees everyone's."""
    manage = _can(ctx.role, "manage_workspace_settings")
    return {"items": await sessions.list_sessions(ctx.workspace_id, None if manage else ctx.user_id), "can_manage": manage}


@router.post("/{session_id}/revoke")
@limiter.limit("30/minute")
async def revoke(request: Request, session_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        found = await sessions.revoke(ctx.workspace_id, session_id, by=ctx.user_id, can_manage=_can(ctx.role, "manage_workspace_settings"))
    except sessions.SessionError as exc:
        raise _fail(exc)
    if not found:
        raise HTTPException(status_code=404, detail="That sign-in was not found.")
    return {"ok": True}
