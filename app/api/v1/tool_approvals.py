"""A person's yes to one publish call made by a key, the CLI or an assistant.

A tool that publishes is never run from outside on the caller's word. The call answers `approval_required`; a person who may publish says
yes here, and gets a one-time token for exactly that tool and that input, good for 15 minutes. The caller repeats the call with the token.
A post the safety check is holding back cannot be approved here."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app.core.middleware import limiter
from app.core.rbac import assert_permission
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.tools import ledger
from app.tools.contract import Risk
from app.tools.gatekeeper import fingerprint
from app.tools.registry import get_registry
from app.tools.services import any_held_by_safety

router = APIRouter()


class Decision(BaseModel):
    approve: bool


def _held(ctx: WorkspaceContext) -> set[str]:
    from app.core.rbac import ROLE_PERMISSIONS

    return set(ROLE_PERMISSIONS.get(ctx.member.get("role", ""), set()))


@router.get("/pending")
@limiter.limit("120/minute")
async def pending(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """The calls from keys, the CLI or an assistant waiting for a yes, with the exact post and where it goes. Only calls this person may
    approve are shown."""
    from app.tools import approvals

    held, registry = _held(ctx), get_registry()
    rows = []
    for r in await approvals.pending(ctx.workspace_id):
        tool = registry.get(r["tool"])
        if tool is not None and (not tool.permission or tool.permission in held):
            rows.append(r)
    return {"requests": rows}


@router.post("/{request_id}/decide")
@limiter.limit("60/minute")
async def decide(request: Request, request_id: str, body: Decision, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """Say yes or no to one waiting call. A yes is one-time and for exactly that call; the caller picks it up with the request's id."""
    from app.tools import approvals

    row = next((r for r in await approvals.pending(ctx.workspace_id, 200) if r["id"] == request_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="That request was not found, or it was already answered.")
    tool = get_registry().get(row["tool"])
    if tool is not None and tool.permission:
        assert_permission(ctx.member, tool.permission)
    return {"request": await approvals.decide(ctx.workspace_id, request_id, ctx.user_id, body.approve)}


class ApprovalBody(BaseModel):
    tool: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.post("", status_code=201)
@limiter.limit("30/minute")
async def approve_call(request: Request, body: ApprovalBody, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    tool = get_registry().get(body.tool)
    if tool is None or tool.risk is not Risk.PUBLISH or tool.approval != "person":
        raise HTTPException(status_code=404, detail="That is not a tool that needs a person's yes.")
    if tool.permission:
        assert_permission(ctx.member, tool.permission)
    try:
        arguments = tool.input_model.model_validate(body.arguments)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=[{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()][:10])
    if await any_held_by_safety(ctx.workspace_id, arguments):
        raise HTTPException(status_code=409, detail="The safety check is holding this back. Review it in Ekavo first.")
    token = await ledger.issue_approval(ctx.workspace_id, ctx.user_id, tool.name, fingerprint(tool.name, arguments.model_dump(mode="json")))
    return {"approval_token": token, "tool": tool.name, "valid_minutes": ledger.APPROVAL_MINUTES}
