"""Support tickets — member-facing routes. A member of any role can file a
ticket for their active workspace and see their workspace's own tickets;
resolving them is staff-only, see app.api.v1.ops_support.
"""

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.db.mongo import support_tickets
from app.models.support import (
    SupportMessage,
    SupportMessageCreate,
    SupportTicket,
    SupportTicketCreate,
)
from app.shared.llm import GroqModel, call_llm, set_usage_workspace

router = APIRouter()
logger = logging.getLogger(__name__)

_ASSISTANT_SYSTEM = (
    "You are the Recast support assistant. Recast is a content workspace: a member "
    "describes an idea once and Recast turns it into platform-ready posts (LinkedIn, "
    "Instagram, Facebook, Threads, Bluesky, YouTube and more) in that workspace's own "
    "brand voice, across text, audio and image pipelines, with review before anything "
    "publishes. Answer the member's question as helpfully and honestly as you can from "
    "that description alone. If the question needs specific account data, live system "
    "status, error codes or billing figures you cannot actually see, say so plainly and "
    "suggest they email hello@recast.so or file a ticket below — never invent a number, "
    "status or error code you don't have."
)


class SupportAssistRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class SupportAssistResponse(BaseModel):
    reply: str


@router.get("/tickets")
@limiter.limit("30/minute")
async def list_my_tickets(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    rows = await support_tickets.find({"workspace_id": ctx.workspace_id}).sort("created_at", -1).to_list(100)
    return {"tickets": [SupportTicket(**r) for r in rows], "total": len(rows)}


@router.post("/tickets")
@limiter.limit("10/minute")
async def create_ticket(
    request: Request, body: SupportTicketCreate, ctx: WorkspaceContext = Depends(get_current_workspace)
) -> SupportTicket:
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid4()),
        "workspace_id": ctx.workspace_id,
        "workspace_name": ctx.workspace.get("name", ""),
        "created_by": ctx.user_id,
        "created_by_name": ctx.user.get("name", ""),
        "subject": body.subject,
        "category": body.category,
        "severity": body.severity.value,
        "status": "open",
        "messages": [
            {
                "sender": "member",
                "sender_name": ctx.user.get("name", ""),
                "text": body.description,
                "created_at": now,
            }
        ],
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
    }
    await support_tickets.insert_one(doc)
    return SupportTicket(**doc)


@router.post("/tickets/{ticket_id}/messages")
@limiter.limit("30/minute")
async def add_message(
    request: Request,
    ticket_id: str,
    body: SupportMessageCreate,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> SupportTicket:
    ticket = await support_tickets.find_one({"id": ticket_id, "workspace_id": ctx.workspace_id})
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    now = datetime.now(timezone.utc)
    message = SupportMessage(
        sender="member",
        sender_name=ctx.user.get("name", ""),
        text=body.text,
        created_at=now,
    )
    await support_tickets.update_one(
        {"id": ticket_id},
        {
            "$push": {"messages": message.model_dump()},
            "$set": {"updated_at": now, "status": "open" if ticket["status"] == "resolved" else ticket["status"]},
        },
    )
    doc = await support_tickets.find_one({"id": ticket_id})
    return SupportTicket(**doc)


@router.post("/assist")
@limiter.limit("15/minute")
async def assist(
    request: Request, body: SupportAssistRequest, ctx: WorkspaceContext = Depends(get_current_workspace)
) -> SupportAssistResponse:
    set_usage_workspace(ctx.workspace_id)
    try:
        reply = await call_llm(
            body.message,
            system=_ASSISTANT_SYSTEM,
            model=GroqModel.FAST,
            max_tokens=400,
        )
    except Exception:
        logger.warning("Support assistant call failed for workspace %s", ctx.workspace_id, exc_info=True)
        raise HTTPException(
            status_code=503, detail="Couldn't reach the assistant right now. Try again, or file a ticket below."
        )
    return SupportAssistResponse(reply=reply)
