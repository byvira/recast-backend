"""Ops: the platform layer. Usage, every call, the switches (all tools, one tool, one workspace), every key and the abuse watch.

Reading needs view on the Platform area, any change needs edit (see `area_guard`), and switching every tool off at once needs the Master
Admin. Each change is written to the staff audit with who, what, before, after and the reason given."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.staff import access_for, audit
from app.tools import ops

router = APIRouter()


class Reason(BaseModel):
    reason: str = Field("", max_length=300)


class GlobalSwitch(Reason):
    off: bool
    message: Optional[str] = Field(None, max_length=200)


class ToolSwitch(Reason):
    off: bool
    message: Optional[str] = Field(None, max_length=200)


class WorkspaceSwitch(Reason):
    all_off: bool = False
    off_tools: list[str] = Field(default_factory=list, max_length=400)


@router.get("/overview")
@limiter.limit("60/minute")
async def overview(request: Request, hours: int = Query(24, ge=1, le=720)) -> dict[str, Any]:
    return await ops.overview(hours)


@router.get("/calls")
@limiter.limit("120/minute")
async def calls(
    request: Request, workspace_id: Optional[str] = Query(None, max_length=64), tool: Optional[str] = Query(None, max_length=80),
    outcome: Optional[str] = Query(None, max_length=20), code: Optional[str] = Query(None, max_length=40),
    credential_id: Optional[str] = Query(None, max_length=64), limit: int = Query(50, ge=1, le=200), before: Optional[datetime] = None,
) -> dict[str, Any]:
    return await ops.calls_page(workspace_id=workspace_id, tool=tool, outcome=outcome, code=code, credential_id=credential_id, limit=limit, before=before)


@router.get("/tools")
@limiter.limit("60/minute")
async def tools(request: Request, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    return {"tools": await ops.tool_table(days), "days": days}


@router.get("/switches")
@limiter.limit("120/minute")
async def switches(request: Request) -> dict[str, Any]:
    return await ops.switch_state()


@router.put("/switches/global")
@limiter.limit("20/minute")
async def switch_global(request: Request, body: GlobalSwitch, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    if (await access_for(user)).get("level") != "master_admin":
        raise HTTPException(status_code=403, detail="Only the Master Admin can switch every tool off or on.")
    changed = await ops.set_global(body.off, body.message)
    await audit(user, "tools.switch_global", "all tools", before=changed["before"], after=changed["after"], reason=body.reason)
    return changed["after"]


@router.put("/switches/tools/{tool}")
@limiter.limit("60/minute")
async def switch_tool(request: Request, tool: str, body: ToolSwitch, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        changed = await ops.set_tool(tool, body.off, body.message)
    except KeyError:
        raise HTTPException(status_code=404, detail="There is no tool with that name.")
    await audit(user, "tools.switch_tool", tool, before=changed["before"], after=changed["after"], reason=body.reason)
    return {"tool": tool, "off": changed["after"]}


@router.put("/switches/surfaces/{surface}")
@limiter.limit("60/minute")
async def switch_surface(request: Request, surface: str, body: ToolSwitch, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Pause or resume the command line, assistants (MCP) or service keys (the API) for everyone, without touching the others."""
    try:
        changed = await ops.set_surface(surface, body.off, body.message)
    except KeyError:
        raise HTTPException(status_code=404, detail="There is no such way in. Choose cli, mcp or api.")
    await audit(user, "tools.switch_surface", surface, before=changed["before"], after=changed["after"], reason=body.reason)
    return {"surface": surface, "off": changed["after"]}


@router.put("/switches/workspaces/{workspace_id}")
@limiter.limit("60/minute")
async def switch_workspace(request: Request, workspace_id: str, body: WorkspaceSwitch, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        changed = await ops.set_workspace(workspace_id, body.all_off, body.off_tools)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"There is no tool called {exc.args[0]!r}.")
    await audit(user, "tools.switch_workspace", workspace_id, before=changed["before"], after=changed["after"], reason=body.reason)
    return changed["after"]


@router.get("/keys")
@limiter.limit("60/minute")
async def keys(request: Request, workspace_id: Optional[str] = Query(None, max_length=64)) -> dict[str, Any]:
    return {"keys": await ops.keys(workspace_id=workspace_id)}


@router.post("/keys/{key_id}/revoke")
@limiter.limit("30/minute")
async def revoke_key(request: Request, key_id: str, body: Reason, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    found = await ops.revoke_key(key_id)
    if not found:
        raise HTTPException(status_code=404, detail="That key was not found, or it is already switched off.")
    await audit(user, "tools.revoke_key", f"{found['workspace_id']}:{key_id}", before={"revoked": False}, after={"revoked": True}, reason=body.reason)
    return {"revoked": True, "key_id": key_id}


class RoleChange(BaseModel):
    model: Optional[str] = Field(None, max_length=60)
    max_tokens: Optional[int] = Field(None, ge=100, le=4000)
    reasoning_effort: Optional[str] = Field(None, max_length=10)


class RouterChange(Reason):
    roles: dict[str, RoleChange] = Field(min_length=1, max_length=4)


def _role_view(role: Any) -> dict[str, Any]:
    return {"name": role.name, "model": role.model, "max_tokens": role.max_tokens, "reasoning_effort": role.reasoning_effort, "private": role.private}


@router.get("/ask-router")
@limiter.limit("60/minute")
async def ask_router(request: Request) -> dict[str, Any]:
    """The four jobs Ask Ekavo gives to models (decide, plan, respond, explain), with the model, token budget and effort each uses now, the defaults,
    and the models a job may use. Every job is private content: it stays with the main provider and its approved backups."""
    from app.ask import router as ask_models

    current = await ask_models.roles()
    return {"roles": [_role_view(r) for r in current.values()], "defaults": [_role_view(r) for r in ask_models.DEFAULTS.values()], "allowed_models": sorted(ask_models.ALLOWED_MODELS)}


@router.put("/ask-router")
@limiter.limit("20/minute")
async def change_ask_router(request: Request, body: RouterChange, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    from app.ask import router as ask_models

    before = {n: _role_view(r) for n, r in (await ask_models.roles()).items()}
    try:
        after = await ask_models.save_roles({n: c.model_dump(exclude_none=True) for n, c in body.roles.items()})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await audit(user, "tools.ask_router", "ask router", before=before, after={n: _role_view(r) for n, r in after.items()}, reason=body.reason)
    return {"roles": [_role_view(r) for r in after.values()]}


class ShadowChange(BaseModel):
    name: Optional[str] = Field(None, max_length=40)
    reason: str = Field("", max_length=300)


@router.get("/ask-shadow")
@limiter.limit("60/minute")
async def ask_shadow(request: Request) -> dict[str, Any]:
    """The candidate deciders that can run beside the live one, which is on, and how often each agreed with the live choice over the last week."""
    from app.ask import decisions

    return {"candidates": sorted(decisions.CANDIDATES), "current": await decisions.shadow_name(), "agreement": await decisions.agreement()}


@router.put("/ask-shadow")
@limiter.limit("20/minute")
async def change_ask_shadow(request: Request, body: ShadowChange, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    from app.ask import decisions

    before = await decisions.shadow_name()
    try:
        after = await decisions.set_shadow(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await audit(user, "tools.ask_shadow", "ask shadow", before=before, after=after, reason=body.reason)
    return {"candidates": sorted(decisions.CANDIDATES), "current": after, "agreement": await decisions.agreement()}


@router.get("/abuse")
@limiter.limit("60/minute")
async def abuse(request: Request, hours: int = Query(1, ge=1, le=24)) -> dict[str, Any]:
    return await ops.abuse(hours)


@router.get("/audit")
@limiter.limit("60/minute")
async def audit_log(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    from app.core.staff import audit_col

    rows = await audit_col().find({"action": {"$regex": "^tools\\."}}, {"_id": 0}).sort("at", -1).limit(limit).to_list(length=limit)
    return {"items": [{**r, "at": r["at"].isoformat().replace("+00:00", "Z")} for r in rows]}
