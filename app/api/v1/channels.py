"""Chat apps as a member sees them: which are available, the chats linked to you, a one-time code to link a new one, and unlinking.

A chat acts as the member who linked it, with their role (see `app/channels/`). Linking needs the right to create content."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.channels import link as links
from app.channels import telegram, whatsapp
from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require

router = APIRouter()
_CREATE = require("create_content")


class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["whatsapp", "telegram"]


def available() -> list[dict[str, Any]]:
    """The chat apps this deployment answers on, with what a person needs to find the bot."""
    out = []
    if whatsapp.enabled():
        out.append({"channel": "whatsapp", "label": "WhatsApp", "address": settings.WHATSAPP_DISPLAY_NUMBER or None})
    if telegram.enabled():
        out.append({"channel": "telegram", "label": "Telegram", "address": f"@{settings.TELEGRAM_BOT_USERNAME}" if settings.TELEGRAM_BOT_USERNAME else None})
    return out


@router.get("")
@limiter.limit("60/minute")
async def mine(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return {"available": available(), "links": await links.list_for(ctx.workspace_id, ctx.user_id), "max": links.MAX_LINKS_PER_MEMBER, "code_minutes": links.CODE_MINUTES}


@router.post("/link-code", status_code=201)
@limiter.limit("10/minute")
async def link_code(request: Request, body: CodeIn, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    if body.channel not in {c["channel"] for c in available()}:
        raise HTTPException(status_code=409, detail="That chat app is not switched on yet.")
    try:
        return await links.make_code(ctx.workspace_id, ctx.user_id, body.channel)
    except links.LinkError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


@router.delete("/{link_id}")
@limiter.limit("30/minute")
async def unlink(request: Request, link_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, bool]:
    if not await links.unlink(link_id, ctx.workspace_id, ctx.user_id):
        raise HTTPException(status_code=404, detail="That chat was not found.")
    return {"ok": True}
