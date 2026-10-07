"""Support tickets — member-facing routes. A member files tickets for their
active workspace and sees their own; resolving them is staff-only, see
app.api.v1.ops_support.

Every write goes through ``_own_ticket``. Everything a member reads goes
through ``member_view`` so internal notes and staff-only fields never leave.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.db.mongo import (
    support_files,
    support_notifications,
    support_ticket_events,
    support_tickets,
    workspaces,
)
from app.models.support import (
    SupportMessage,
    SupportMessageCreate,
    SupportRatingBody,
    SupportTicketCloseRequest,
    SupportTicketCreate,
    SupportTicketMemberView,
)
from app.shared import support_files as files_service
from app.shared import support_guides
from app.shared import support_privacy
from app.shared import support_rules as rules
from app.shared.support_log import support_request_log
from app.shared import support_sla as sla
from app.shared.support import log_event, member_view, next_ticket_number, transition_fields
from app.shared.support_context import enrich_ticket
from app.shared.support_triage import triage_ticket
from app.shared.support_notify import (
    mark_ticket_read,
    notify_member,
    notify_ops_new_ticket,
    notify_staff_user,
    spawn,
    unread_count,
)

router = APIRouter(dependencies=[Depends(support_request_log)])
logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ["open", "investigating", "waiting_on_member", "waiting_on_engineering"]


def _can_read_team_tickets(ctx: WorkspaceContext) -> bool:
    """Owners and admins may read teammates' tickets, but only when the
    workspace has switched that on. Off by default."""
    return bool(ctx.workspace.get("support_admins_can_view_tickets")) and ctx.role in ("owner", "admin")


async def _own_ticket(ticket_id: str, ctx: WorkspaceContext) -> dict:
    """A ticket the caller filed. Anything else is a 404, never a 403, so a
    guessed id reveals nothing about whether it exists. Every write goes
    through this."""
    ticket = await support_tickets.find_one(
        {"id": ticket_id, "workspace_id": ctx.workspace_id, "created_by": ctx.user_id}
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return ticket


async def _readable_ticket(ticket_id: str, ctx: WorkspaceContext) -> dict:
    """Own ticket, or a teammate's when the workspace allows admins to read them."""
    query: dict = {"id": ticket_id, "workspace_id": ctx.workspace_id}
    if not _can_read_team_tickets(ctx):
        query["created_by"] = ctx.user_id
    ticket = await support_tickets.find_one(query)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return ticket


def _too_many(detail: str) -> HTTPException:
    return HTTPException(status_code=429, detail=detail)


async def _enforce_ticket_limits(ctx: WorkspaceContext) -> None:
    now = datetime.now(timezone.utc)
    if await support_tickets.count_documents(
        {"created_by": ctx.user_id, "created_at": {"$gte": now - timedelta(hours=1)}}
    ) >= rules.MAX_TICKETS_PER_MEMBER_PER_HOUR:
        raise _too_many(
            "You have filed several tickets in the last hour. Please add to an existing ticket, or try again later."
        )
    if await support_tickets.count_documents(
        {"workspace_id": ctx.workspace_id, "created_at": {"$gte": now - timedelta(days=1)}}
    ) >= rules.MAX_TICKETS_PER_WORKSPACE_PER_DAY:
        raise _too_many("Your workspace has reached today's ticket limit. Please add to an existing ticket.")


async def _enforce_message_limit(ctx: WorkspaceContext) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    sent = await support_ticket_events.count_documents(
        {
            "actor_id": ctx.user_id,
            "actor_type": "member",
            "type": {"$in": ["replied", "reopened"]},
            "created_at": {"$gte": since},
        }
    )
    if sent >= rules.MAX_MESSAGES_PER_MEMBER_PER_HOUR:
        raise _too_many("You have sent a lot of messages in the last hour. Please wait a little before sending more.")


async def _claim_or_400(file_ids: list[str], ctx: WorkspaceContext, ticket_id: str) -> list[dict]:
    try:
        return await files_service.claim(file_ids, uploader_id=ctx.user_id, ticket_id=ticket_id)
    except files_service.AttachmentRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/settings")
@limiter.limit("30/minute")
async def support_settings(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    return {
        "admins_can_view_tickets": bool(ctx.workspace.get("support_admins_can_view_tickets")),
        "can_manage": ctx.role == "owner",
        "can_view_team_tickets": _can_read_team_tickets(ctx),
    }


class SupportSettingsUpdate(BaseModel):
    admins_can_view_tickets: bool


@router.put("/settings")
@limiter.limit("10/minute")
async def update_support_settings(
    request: Request, body: SupportSettingsUpdate, ctx: WorkspaceContext = Depends(get_current_workspace)
) -> dict:
    """Owner only: let workspace admins read teammates' tickets (read-only)."""
    if ctx.role != "owner":
        raise HTTPException(status_code=403, detail="Only the workspace owner can change this.")
    await workspaces.update_one(
        {"id": ctx.workspace_id}, {"$set": {"support_admins_can_view_tickets": body.admins_can_view_tickets}}
    )
    return {
        "admins_can_view_tickets": body.admins_can_view_tickets,
        "can_manage": True,
        "can_view_team_tickets": body.admins_can_view_tickets,
    }


@router.get("/tickets")
@limiter.limit("30/minute")
async def list_my_tickets(
    request: Request,
    view: str = Query(default="all", pattern="^(all|active|resolved)$"),
    scope: str = Query(default="mine", pattern="^(mine|workspace)$"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    query: dict = {"workspace_id": ctx.workspace_id}
    if scope == "workspace":
        if not _can_read_team_tickets(ctx):
            raise HTTPException(status_code=403, detail="Your workspace hasn't turned on team ticket access.")
    else:
        query["created_by"] = ctx.user_id
    if view == "active":
        query["status"] = {"$in": _ACTIVE_STATUSES}
    elif view == "resolved":
        query["status"] = {"$in": ["resolved", "closed"]}
    total = await support_tickets.count_documents(query)
    rows = await support_tickets.find(query).sort("updated_at", -1).skip(skip).limit(limit).to_list(limit)
    # The list only needs the latest message for its preview.
    return {"tickets": [member_view(r, viewer_id=ctx.user_id, limit=1) for r in rows], "total": total}


@router.get("/tickets/{ticket_id}")
@limiter.limit("60/minute")
async def get_my_ticket(
    request: Request,
    ticket_id: str,
    limit: int = Query(default=rules.MESSAGE_PAGE_SIZE, ge=1, le=100),
    before: Optional[int] = Query(default=None, ge=0),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> SupportTicketMemberView:
    ticket = await _readable_ticket(ticket_id, ctx)
    if ticket["created_by"] == ctx.user_id:
        if ticket.get("unread_for_member"):
            await support_tickets.update_one({"id": ticket_id}, {"$set": {"unread_for_member": False}})
            ticket["unread_for_member"] = False
        await mark_ticket_read(ctx.user_id, ticket_id)
    return member_view(ticket, viewer_id=ctx.user_id, limit=limit, before=before)


@router.post("/uploads")
@limiter.limit("30/minute")
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    ticket_id: Optional[str] = Form(default=None),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Store one file privately and return its id, to be attached to a ticket
    or reply. A file that fails the check is refused; if it was meant for a
    ticket, staff are told on that ticket."""
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    if (
        await support_files.count_documents({"uploaded_by": ctx.user_id, "created_at": {"$gte": since}})
        >= rules.MAX_UPLOADS_PER_MEMBER_PER_HOUR
    ):
        raise _too_many("You have uploaded a lot of files in the last hour. Please try again later.")

    data = await file.read(max(rules.MAX_ATTACHMENT_BYTES, rules.MAX_VIDEO_BYTES) + 1)
    try:
        doc = await files_service.store(
            uploader_id=ctx.user_id,
            uploader_type="member",
            workspace_id=ctx.workspace_id,
            filename=file.filename or "file",
            declared_mime=file.content_type,
            data=data,
        )
    except files_service.AttachmentRejected as exc:
        if ticket_id:
            ticket = await support_tickets.find_one(
                {"id": ticket_id, "workspace_id": ctx.workspace_id, "created_by": ctx.user_id}
            )
            if ticket:
                now = datetime.now(timezone.utc)
                note = {
                    "sender": "system",
                    "sender_name": "Recast",
                    "is_internal": True,
                    "created_at": now,
                    "text": f"The member tried to attach a file ({(file.filename or 'file')[:80]}) and it was blocked: {exc}",
                    "attachments": [],
                }
                await support_tickets.update_one(
                    {"id": ticket_id},
                    {"$push": {"messages": note}, "$set": {"updated_at": now, "unread_for_ops": True}},
                )
                await log_event(
                    ticket_id, actor_type="system", actor_id=None, actor_name="Recast",
                    type="attachment_blocked", data={"reason": str(exc)},
                )
        raise HTTPException(status_code=422, detail=str(exc))
    return {"file_id": doc["id"], "name": doc["name"], "mime": doc["mime"], "size": doc["size"]}


@router.get("/attachments/{file_id}")
@limiter.limit("60/minute")
async def download_attachment(
    request: Request, file_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)
) -> dict:
    """A short-lived signed link. Access is checked on every request: the file
    must be on a ticket the caller may read, in a message the member can see
    (never one attached to an internal note)."""
    file_doc = await support_files.find_one({"id": file_id})
    if not file_doc or not file_doc.get("ticket_id"):
        raise HTTPException(status_code=404, detail="File not found.")
    ticket = await _readable_ticket(file_doc["ticket_id"], ctx)
    on_visible_message = any(
        not m.get("is_internal", False) and any(a.get("file_id") == file_id for a in m.get("attachments", []))
        for m in ticket.get("messages", [])
    )
    if not on_visible_message:
        raise HTTPException(status_code=404, detail="File not found.")
    return await files_service.signed_link(file_doc)


@router.post("/tickets")
@limiter.limit("10/minute")
async def create_ticket(
    request: Request, body: SupportTicketCreate, ctx: WorkspaceContext = Depends(get_current_workspace)
) -> SupportTicketMemberView:
    if ctx.user.get("support_muted"):
        raise HTTPException(
            status_code=403, detail="Filing tickets is paused for your account. Please email hello@recast.so."
        )
    await _enforce_ticket_limits(ctx)
    now = datetime.now(timezone.utc)

    if not body.allow_duplicate:
        existing = await support_tickets.find_one(
            {
                "created_by": ctx.user_id,
                "workspace_id": ctx.workspace_id,
                "category": body.category,
                "status": {"$in": _ACTIVE_STATUSES},
                "created_at": {"$gte": now - rules.DUPLICATE_TICKET_WINDOW},
            }
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "possible_duplicate",
                    "message": "You already have an open ticket about this. Add to it instead?",
                    "existing_ticket_id": existing["id"],
                },
            )

    ticket_id = str(uuid4())
    attachments = await _claim_or_400(body.attachment_ids, ctx, ticket_id)
    doc = {
        "id": ticket_id,
        "number": await next_ticket_number(),
        "workspace_id": ctx.workspace_id,
        "workspace_name": ctx.workspace.get("name", ""),
        "workspace_tier": ctx.workspace.get("tier"),
        "created_by": ctx.user_id,
        "created_by_name": ctx.user.get("name", ""),
        "created_by_email": ctx.user.get("email"),
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
                "is_internal": False,
                "attachments": attachments,
            }
        ],
        "assignee_id": None,
        "assignee_name": None,
        "tags": [],
        "related_ticket_id": body.related_ticket_id,
        "source_context": body.source_context.model_dump() if body.source_context else None,
        "client_env": body.client_env.model_dump(exclude_none=True) if body.client_env else None,
        "snoozed_until": None,
        "sla": sla.compute(now, body.severity.value, ctx.workspace.get("tier"), await sla.get_config()),
        "unread_for_member": False,
        "unread_for_ops": True,
        "last_member_message_at": now,
        "last_staff_message_at": None,
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
        "closed_at": None,
    }
    await support_tickets.insert_one(doc)
    await log_event(
        doc["id"], actor_type="member", actor_id=ctx.user_id, actor_name=doc["created_by_name"],
        type="created", data={"category": body.category, "severity": body.severity.value},
    )
    # Receipt to the member, heads-up to staff, and the context snapshot,
    # none of which may slow down or fail the ticket itself.
    await notify_member(
        doc, "ticket_created", "We got your ticket",
        "A real person will reply here and by email. You can add more detail any time.",
    )
    await notify_ops_new_ticket(doc)
    spawn(enrich_ticket(doc["id"]))
    spawn(triage_ticket(doc["id"]))
    return member_view(doc, viewer_id=ctx.user_id)


@router.post("/tickets/{ticket_id}/messages")
@limiter.limit("30/minute")
async def add_message(
    request: Request,
    ticket_id: str,
    body: SupportMessageCreate,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> SupportTicketMemberView:
    ticket = await _own_ticket(ticket_id, ctx)
    if ticket["status"] == "closed":
        raise HTTPException(
            status_code=409,
            detail="This ticket is closed. Open a new ticket and mention this one.",
        )
    await _enforce_message_limit(ctx)

    now = datetime.now(timezone.utc)
    attachments = await _claim_or_400(body.attachment_ids, ctx, ticket_id)
    message = SupportMessage(
        sender="member", sender_name=ctx.user.get("name", ""), text=body.text, created_at=now,
        attachments=attachments,
    )
    set_fields: dict = {
        "updated_at": now,
        "last_member_message_at": now,
        "unread_for_ops": True,
    }
    # A reply on a resolved ticket reopens it; a reply to a question moves it
    # back to the person working on it.
    reopen_from = ticket["status"] if ticket["status"] in ("resolved", "waiting_on_member") else None
    if reopen_from:
        set_fields.update(transition_fields(reopen_from, "investigating", now))
        set_fields["reminder_sent_at"] = None
        set_fields["close_warning_sent_at"] = None
    await support_tickets.update_one(
        {"id": ticket_id}, {"$push": {"messages": message.model_dump()}, "$set": set_fields}
    )
    await log_event(
        ticket_id, actor_type="member", actor_id=ctx.user_id, actor_name=ctx.user.get("name", ""),
        type="reopened" if reopen_from == "resolved" else "replied",
    )
    if ticket.get("assignee_id"):
        await notify_staff_user(
            ticket["assignee_id"], ticket, "member_replied",
            f"Reply on #{ticket.get('number') or ticket['id'][:6]}", body.text,
        )
    return member_view(
        await support_tickets.find_one({"id": ticket_id}), viewer_id=ctx.user_id, limit=rules.MESSAGE_PAGE_SIZE
    )


@router.post("/tickets/{ticket_id}/reopen")
@limiter.limit("10/minute")
async def reopen_ticket(
    request: Request, ticket_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)
) -> SupportTicketMemberView:
    """The member says a resolved ticket isn't actually solved."""
    ticket = await _own_ticket(ticket_id, ctx)
    if ticket["status"] != "resolved":
        raise HTTPException(status_code=409, detail="Only a resolved ticket can be reopened.")
    now = datetime.now(timezone.utc)
    fields = transition_fields(ticket["status"], "investigating", now)
    await support_tickets.update_one(
        {"id": ticket_id},
        {"$set": {**fields, "updated_at": now, "unread_for_ops": True, "close_warning_sent_at": None}},
    )
    await log_event(
        ticket_id, actor_type="member", actor_id=ctx.user_id, actor_name=ctx.user.get("name", ""),
        type="reopened",
    )
    return member_view(
        await support_tickets.find_one({"id": ticket_id}), viewer_id=ctx.user_id, limit=rules.MESSAGE_PAGE_SIZE
    )


@router.post("/tickets/{ticket_id}/close")
@limiter.limit("10/minute")
async def close_ticket(
    request: Request,
    ticket_id: str,
    body: SupportTicketCloseRequest,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> SupportTicketMemberView:
    """Withdraw an open ticket, or confirm a resolved one is solved.

    The record stays (staff and the audit trail keep it); the member just
    stops seeing it as active. There is deliberately no hard delete.
    """
    ticket = await _own_ticket(ticket_id, ctx)
    now = datetime.now(timezone.utc)
    fields = transition_fields(ticket["status"], "closed", now)
    reason = "confirmed_resolved" if ticket["status"] == "resolved" else "withdrawn"
    await support_tickets.update_one(
        {"id": ticket_id},
        {"$set": {**fields, "closed_reason": reason, "updated_at": now, "unread_for_ops": True}},
    )
    await log_event(
        ticket_id, actor_type="member", actor_id=ctx.user_id, actor_name=ctx.user.get("name", ""),
        type="closed", data={"reason": reason, "note": body.reason},
    )
    if ticket.get("assignee_id"):
        await notify_staff_user(
            ticket["assignee_id"], ticket, "member_closed",
            f"Ticket #{ticket.get('number') or ticket['id'][:6]} closed by the member",
            "They said it is sorted." if reason == "confirmed_resolved" else "They withdrew it.",
        )
    return member_view(
        await support_tickets.find_one({"id": ticket_id}), viewer_id=ctx.user_id, limit=rules.MESSAGE_PAGE_SIZE
    )


@router.post("/tickets/{ticket_id}/rating")
@limiter.limit("20/minute")
async def rate_ticket(
    request: Request,
    ticket_id: str,
    body: SupportRatingBody,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> SupportTicketMemberView:
    """Thumbs up or down, with an optional comment, once a ticket is finished.
    A member can change their rating."""
    ticket = await _own_ticket(ticket_id, ctx)
    if ticket["status"] not in ("resolved", "closed"):
        raise HTTPException(status_code=409, detail="You can rate a ticket once it is resolved.")
    now = datetime.now(timezone.utc)
    rating = {"value": body.value, "comment": (body.comment or "").strip() or None, "at": now}
    await support_tickets.update_one({"id": ticket_id}, {"$set": {"rating": rating, "updated_at": now}})
    await log_event(
        ticket_id, actor_type="member", actor_id=ctx.user_id, actor_name=ctx.user.get("name", ""),
        type="rated", data={"value": body.value},
    )
    return member_view(
        await support_tickets.find_one({"id": ticket_id}), viewer_id=ctx.user_id, limit=rules.MESSAGE_PAGE_SIZE
    )


@router.get("/my-data/export")
@limiter.limit("5/hour")
async def export_my_support_data(
    request: Request, format: Literal["json", "csv"] = "json", ctx: WorkspaceContext = Depends(get_current_workspace),
):
    """A copy of the caller's own support data: their tickets, the messages they could see, ratings and file names."""
    data = await support_privacy.export_member_data(ctx.user_id)
    if format == "csv":
        from fastapi.responses import Response

        return Response(
            support_privacy.export_to_csv(data), media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="my-support-data.csv"'},
        )
    return data


class SupportEraseRequest(BaseModel):
    # Must be true. A deliberate second step, because this cannot be undone.
    confirm: bool


@router.post("/my-data/erase")
@limiter.limit("3/hour")
async def erase_my_support_data(
    request: Request, body: SupportEraseRequest, ctx: WorkspaceContext = Depends(get_current_workspace)
) -> dict:
    """Delete the caller's support data: their tickets (any still open are
    closed first), messages, files, notifications and chats. Only counts
    remain, with no words and no name. Cannot be undone."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Please confirm that you want your support data deleted.")
    return await support_privacy.erase_member_data(ctx.user_id)


@router.get("/guides")
@limiter.limit("60/minute")
async def guides(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """The help guides, with their topics. One source for the Support page and
    for what staff drafts quote."""
    return {
        "topics": support_guides.TOPICS,
        "guides": [
            {k: g[k] for k in ("id", "topic", "title", "summary", "body")} for g in support_guides.GUIDES
        ],
    }


@router.get("/notifications")
@limiter.limit("60/minute")
async def my_notifications(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """Unread count (for the badge) and the latest updates about my tickets."""
    rows = (
        await support_notifications.find({"user_id": ctx.user_id, "audience": "member"})
        .sort("created_at", -1)
        .to_list(20)
    )
    return {
        "unread": await unread_count(ctx.user_id, "member"),
        "notifications": [
            {
                "id": r["id"], "type": r["type"], "ticket_id": r["ticket_id"], "title": r["title"],
                "body": r["body"], "read": r["read"], "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@router.post("/notifications/read")
@limiter.limit("30/minute")
async def read_my_notifications(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    await support_notifications.update_many(
        {"user_id": ctx.user_id, "audience": "member", "read": False}, {"$set": {"read": True}}
    )
    return {"unread": 0}
