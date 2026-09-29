"""Support tickets — staff-facing routes. Cross-tenant (every workspace's
tickets, not just one), same platform-staff gate as the Ops LLM Health page.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import support_tickets
from app.models.support import (
    SupportMessage,
    SupportMessageCreate,
    SupportTicket,
    SupportTicketStatus,
    SupportTicketStatusUpdate,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/tickets")
@limiter.limit("30/minute")
async def list_all_tickets(
    request: Request,
    status: SupportTicketStatus | None = Query(default=None),
    staff: dict = Depends(require_platform_staff),
) -> dict:
    query: dict = {}
    if status is not None:
        query["status"] = status.value
    rows = await support_tickets.find(query).sort("created_at", -1).to_list(200)
    return {"tickets": [SupportTicket(**r) for r in rows], "total": len(rows)}


@router.post("/tickets/{ticket_id}/messages")
@limiter.limit("30/minute")
async def staff_add_message(
    request: Request,
    ticket_id: str,
    body: SupportMessageCreate,
    staff: dict = Depends(require_platform_staff),
) -> SupportTicket:
    ticket = await support_tickets.find_one({"id": ticket_id})
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    now = datetime.now(timezone.utc)
    message = SupportMessage(
        sender="staff",
        sender_name=staff.get("name", "Recast Support"),
        text=body.text,
        created_at=now,
    )
    new_status = "investigating" if ticket["status"] == "open" else ticket["status"]
    await support_tickets.update_one(
        {"id": ticket_id},
        {"$push": {"messages": message.model_dump()}, "$set": {"updated_at": now, "status": new_status}},
    )
    doc = await support_tickets.find_one({"id": ticket_id})
    return SupportTicket(**doc)


@router.patch("/tickets/{ticket_id}/status")
@limiter.limit("30/minute")
async def update_ticket_status(
    request: Request,
    ticket_id: str,
    body: SupportTicketStatusUpdate,
    staff: dict = Depends(require_platform_staff),
) -> SupportTicket:
    now = datetime.now(timezone.utc)
    update: dict = {"status": body.status.value, "updated_at": now}
    if body.status == SupportTicketStatus.RESOLVED:
        update["resolved_at"] = now
    res = await support_tickets.update_one({"id": ticket_id}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    doc = await support_tickets.find_one({"id": ticket_id})
    return SupportTicket(**doc)
