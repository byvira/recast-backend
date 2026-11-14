"""Conversations with Ask Ekavo, as the signed-in member sees them: their own list, one conversation with its messages, and removing one.

A conversation is private to the person who started it today (see `app/ask/threads.py`, which is built so a group can be added later). Sending a message
into a conversation and Ekavo's replies come with the next stage; this reads and manages what exists."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.ask import threads
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace

router = APIRouter()


class NewThread(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field("", max_length=threads.MAX_TITLE)


def _fail(exc: threads.ThreadError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get("")
@limiter.limit("60/minute")
async def mine(request: Request, q: str = Query("", max_length=80), limit: int = Query(20, ge=1, le=50), before: Optional[datetime] = Query(None),
               ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return await threads.listing(ctx.workspace_id, ctx.user_id, q=q, limit=limit, before=before)


@router.post("", status_code=201)
@limiter.limit("30/minute")
async def start(request: Request, body: NewThread, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await threads.create(ctx.workspace_id, ctx.user_id, "app", title=body.title)
    except threads.ThreadError as exc:
        raise _fail(exc)


@router.get("/{thread_id}")
@limiter.limit("120/minute")
async def one(request: Request, thread_id: str, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        doc = await threads.load(ctx.workspace_id, ctx.user_id, thread_id)
    except threads.ThreadError as exc:
        raise _fail(exc)
    return {"thread": threads.public(doc), "messages": await threads.page(thread_id, after=after, limit=limit)}


@router.delete("/{thread_id}")
@limiter.limit("30/minute")
async def remove(request: Request, thread_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, bool]:
    try:
        await threads.remove(ctx.workspace_id, ctx.user_id, thread_id)
    except threads.ThreadError as exc:
        raise _fail(exc)
    return {"ok": True}
