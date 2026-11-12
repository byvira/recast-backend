"""Ask Ekavo, as the signed-in member sees it: say what you want, see the plan and its price, say yes, watch it work, stop or take it back.

Every step goes through the same gatekeeper as the public API, with the member's own role, and shows in the same record of changes (tagged as made by
Ask), so each one can be undone from Settings, Connections. Ask can read and create; publishing, approving, deleting and anything that needs the
person's own yes is shown to them and never done for them."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.ask import runs
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.tools.executor import ToolFailure

router = APIRouter()


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=runs.MAX_MESSAGE)
    trust: Literal["suggest", "ask", "act"] = "ask"


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1, max_length=500)


def _fail(failure: ToolFailure) -> HTTPException:
    return HTTPException(status_code=failure.status, detail=failure.message)


@router.post("", status_code=201)
@limiter.limit("20/minute")
async def ask(request: Request, body: AskIn, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.create(ctx.workspace_id, ctx.user_id, body.message, body.trust)
    except ToolFailure as failure:
        raise _fail(failure)


@router.get("")
@limiter.limit("60/minute")
async def history(
    request: Request, q: str = Query("", max_length=80), status: str = Query("", max_length=20), limit: int = Query(20, ge=1, le=50),
    before: Optional[datetime] = Query(None), ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """The person's own requests, newest first: search by words, narrow by status group, page back with `before`."""
    try:
        return await runs.history(ctx.workspace_id, ctx.user_id, limit=limit, q=q, group=status, before=before)
    except ToolFailure as failure:
        raise _fail(failure)


@router.get("/{run_id}")
@limiter.limit("240/minute")
async def one(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.get(ctx.workspace_id, ctx.user_id, run_id)
    except ToolFailure as failure:
        raise _fail(failure)


@router.post("/{run_id}/confirm")
@limiter.limit("30/minute")
async def confirm(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.confirm(ctx.workspace_id, ctx.user_id, run_id)
    except ToolFailure as failure:
        raise _fail(failure)


@router.post("/{run_id}/answer", status_code=201)
@limiter.limit("20/minute")
async def answer(request: Request, run_id: str, body: AnswerIn, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.answer(ctx.workspace_id, ctx.user_id, run_id, body.reply)
    except ToolFailure as failure:
        raise _fail(failure)


@router.post("/{run_id}/stop")
@limiter.limit("60/minute")
async def stop(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.stop(ctx.workspace_id, ctx.user_id, run_id)
    except ToolFailure as failure:
        raise _fail(failure)
