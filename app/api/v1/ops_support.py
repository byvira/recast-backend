"""Support tickets — staff-facing routes. Cross-tenant (every workspace's
tickets, not just one), same platform-staff gate as the Ops LLM Health page.

Every status change goes through ``app.shared.support.transition_fields`` (the
single transition table) and every action writes an audit event.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import (
    support_files,
    support_notifications,
    support_ticket_context,
    support_ticket_events,
    support_tickets,
    users,
    workspaces,
)
from app.models.support import (
    SupportMessage,
    SupportStaffMessageCreate,
    SupportTicket,
    SupportTicketEvent,
    SupportTicketSeverity,
    SupportTicketStaffUpdate,
    SupportTicketStatus,
    SupportTicketStatusUpdate,
)
from app.shared import support_files as files_service
from app.shared import support_rules as rules
from app.shared import support_ai
from app.shared import support_sla as sla
from app.shared.support_log import support_request_log
from app.shared.support import (
    has_role_at_least,
    log_event,
    staff_role,
    transition_fields,
)
from app.shared.support_context import enrich_ticket
from app.shared.support_notify import mark_ticket_read, notify_member, unread_count

router = APIRouter(dependencies=[Depends(support_request_log)])
logger = logging.getLogger(__name__)


def _overdue_filter(now: datetime) -> dict:
    """Not finished, and past a promised time: no first reply by its due time,
    or not resolved by its due time."""
    return {
        "status": {"$nin": ["resolved", "closed"]},
        "$or": [
            {"sla.first_responded_at": None, "sla.first_response_due": {"$lt": now}},
            {"sla.resolve_due": {"$lt": now}},
        ],
    }


def _actor(staff: dict) -> dict:
    return {"actor_type": "staff", "actor_id": staff["id"], "actor_name": staff.get("name", "Recast Support")}


def _summary(row: dict) -> dict:
    """A queue row: the ticket without its whole thread."""
    messages = row.get("messages", [])
    public = [m for m in messages if not m.get("is_internal", False)]
    last = (public or messages)[-1]["text"] if messages else ""
    ticket = SupportTicket(**{**row, "messages": []}).model_dump(mode="json")
    ticket["message_count"] = len(messages)
    ticket["last_message_preview"] = last[:140]
    return ticket


async def _flag_deleted(rows: list[dict]) -> None:
    """Mark tickets whose member or workspace has since been deleted. Staff can
    still read them, but there is no one to reply to."""
    if not rows:
        return
    user_ids = {r["created_by"] for r in rows if r.get("created_by")}
    ws_ids = {r["workspace_id"] for r in rows if r.get("workspace_id")}
    live_users = {u["id"] async for u in users.find({"id": {"$in": list(user_ids)}}, {"id": 1})}
    live_ws = {w["id"] async for w in workspaces.find({"id": {"$in": list(ws_ids)}}, {"id": 1})}
    for r in rows:
        r["created_by_deleted"] = r.get("created_by") not in live_users
        r["workspace_deleted"] = r.get("workspace_id") not in live_ws


async def _get_ticket(ticket_id: str) -> dict:
    ticket = await support_tickets.find_one({"id": ticket_id})
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return ticket


@router.get("/me")
@limiter.limit("60/minute")
async def my_staff_profile(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    return {"id": staff["id"], "name": staff.get("name", ""), "role": staff_role(staff)}


@router.post("/tickets/{ticket_id}/messages")
@limiter.limit("60/minute")
async def staff_add_message(
    request: Request,
    ticket_id: str,
    body: SupportStaffMessageCreate,
    staff: dict = Depends(require_platform_staff),
) -> dict:
    return await _post_staff_message(ticket_id, body, staff)


@router.post("/uploads")
@limiter.limit("30/minute")
async def staff_upload(
    request: Request, file: UploadFile = File(...), staff: dict = Depends(require_platform_staff)
) -> dict:
    data = await file.read(max(rules.MAX_ATTACHMENT_BYTES, rules.MAX_VIDEO_BYTES) + 1)
    try:
        doc = await files_service.store(
            uploader_id=staff["id"], uploader_type="staff", workspace_id="staff",
            filename=file.filename or "file", declared_mime=file.content_type, data=data,
        )
    except files_service.AttachmentRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"file_id": doc["id"], "name": doc["name"], "mime": doc["mime"], "size": doc["size"]}


@router.get("/attachments/{file_id}")
@limiter.limit("60/minute")
async def staff_download(request: Request, file_id: str, staff: dict = Depends(require_platform_staff)) -> dict:
    """Signed, short-lived link. Staff can open any attached file, including
    ones on internal notes; a file that was never attached is not reachable."""
    file_doc = await support_files.find_one({"id": file_id})
    if not file_doc or not file_doc.get("ticket_id"):
        raise HTTPException(status_code=404, detail="File not found.")
    return await files_service.signed_link(file_doc)


@router.post("/tickets/{ticket_id}/enrich")
@limiter.limit("10/minute")
async def retry_enrich(request: Request, ticket_id: str, staff: dict = Depends(require_platform_staff)) -> dict:
    """"Context unavailable, retry": rebuild the snapshot now."""
    await _get_ticket(ticket_id)
    context = await enrich_ticket(ticket_id)
    context.pop("_id", None)
    return {"context": context}


@router.get("/notifications")
@limiter.limit("60/minute")
async def staff_notifications(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    rows = (
        await support_notifications.find({"user_id": staff["id"], "audience": "ops"})
        .sort("created_at", -1)
        .to_list(30)
    )
    return {
        "unread": await unread_count(staff["id"], "ops"),
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
async def read_staff_notifications(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    await support_notifications.update_many(
        {"user_id": staff["id"], "audience": "ops", "read": False}, {"$set": {"read": True}}
    )
    return {"unread": 0}


@router.get("/staff")
@limiter.limit("60/minute")
async def list_support_staff(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    """Who a ticket can be assigned to."""
    rows = await users.find(
        {"$or": [{"is_platform_staff": True}, {"is_master_admin": True}]},
        {"id": 1, "name": 1, "is_master_admin": 1, "support_role": 1},
    ).to_list(100)
    return {"staff": [{"id": r["id"], "name": r.get("name", ""), "role": staff_role(r)} for r in rows]}


@router.get("/stats")
@limiter.limit("60/minute")
async def queue_stats(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    """The numbers on the queue's stat cards, counted across the whole queue
    (not just the page of rows currently loaded)."""
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    unassigned = {
        "$or": [{"assignee_id": None}, {"assignee_id": {"$exists": False}}],
        "status": {"$nin": ["resolved", "closed"]},
    }
    return {
        "unassigned": await support_tickets.count_documents(unassigned),
        "mine": await support_tickets.count_documents(
            {"assignee_id": staff["id"], "status": {"$nin": ["resolved", "closed"]}}
        ),
        "waiting_on_engineering": await support_tickets.count_documents({"status": "waiting_on_engineering"}),
        "resolved_today": await support_tickets.count_documents({"resolved_at": {"$gte": day_start}}),
        "unread": await support_tickets.count_documents({"unread_for_ops": True, "status": {"$ne": "closed"}}),
        "overdue": await support_tickets.count_documents(_overdue_filter(datetime.now(timezone.utc))),
    }


@router.get("/tickets")
@limiter.limit("60/minute")
async def list_all_tickets(
    request: Request,
    status: Optional[SupportTicketStatus] = Query(default=None),
    view: str = Query(default="all", pattern="^(all|unassigned|mine|snoozed|overdue)$"),
    severity: Optional[SupportTicketSeverity] = Query(default=None),
    category: Optional[str] = Query(default=None, max_length=80),
    assignee: Optional[str] = Query(default=None, max_length=80),
    workspace_id: Optional[str] = Query(default=None, max_length=80),
    plan: Optional[str] = Query(default=None, max_length=30),
    platform: Optional[str] = Query(default=None, max_length=40),
    unread: bool = Query(default=False),
    incident_id: Optional[str] = Query(default=None, max_length=80),
    created_from: Optional[datetime] = Query(default=None),
    created_to: Optional[datetime] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=100),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    staff: dict = Depends(require_platform_staff),
) -> dict:
    now = datetime.now(timezone.utc)
    clauses: list[dict] = []
    if status is not None:
        clauses.append({"status": status.value})
    if severity is not None:
        clauses.append({"severity": severity.value})
    if category:
        clauses.append({"category": category})
    if workspace_id:
        clauses.append({"workspace_id": workspace_id})
    if plan:
        clauses.append({"workspace_tier": plan})
    if platform:
        clauses.append({"source_context.type": "platform", "source_context.id": platform})
    if unread:
        clauses.append({"unread_for_ops": True})
    if incident_id:
        clauses.append({"incident_id": incident_id})
    if assignee:
        clauses.append({"assignee_id": assignee})
    if created_from or created_to:
        window: dict = {}
        if created_from:
            window["$gte"] = created_from
        if created_to:
            window["$lte"] = created_to
        clauses.append({"created_at": window})

    not_snoozed = {"$or": [{"snoozed_until": None}, {"snoozed_until": {"$exists": False}}, {"snoozed_until": {"$lte": now}}]}
    if view == "snoozed":
        clauses.append({"snoozed_until": {"$gt": now}})
    else:
        # A snoozed ticket stays out of the working queue until its time.
        clauses.append(not_snoozed)
    if view == "unassigned":
        clauses.append({"$or": [{"assignee_id": None}, {"assignee_id": {"$exists": False}}]})
        clauses.append({"status": {"$nin": ["resolved", "closed"]}})
    elif view == "mine":
        clauses.append({"assignee_id": staff["id"]})
    elif view == "overdue":
        clauses.append(_overdue_filter(now))

    if q and q.strip():
        term = q.strip().lstrip("#")
        pattern = {"$regex": re.escape(term), "$options": "i"}
        text_clauses: list[dict] = [
            {"subject": pattern},
            {"created_by_name": pattern},
            {"created_by_email": pattern},
            {"workspace_name": pattern},
            {"messages.text": pattern},
        ]
        if term.isdigit():
            text_clauses.append({"number": int(term)})
        clauses.append({"$or": text_clauses})

    query: dict = {"$and": clauses} if clauses else {}
    total = await support_tickets.count_documents(query)
    rows = await support_tickets.find(query).sort("updated_at", -1).skip(skip).limit(limit).to_list(limit)
    await _flag_deleted(rows)
    return {"tickets": [_summary(r) for r in rows], "total": total}


@router.get("/tickets/{ticket_id}")
@limiter.limit("60/minute")
async def get_ticket(
    request: Request,
    ticket_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    before: Optional[int] = Query(default=None, ge=0),
    staff: dict = Depends(require_platform_staff),
) -> dict:
    ticket = await _get_ticket(ticket_id)
    await _flag_deleted([ticket])
    if ticket.get("unread_for_ops"):
        await support_tickets.update_one({"id": ticket_id}, {"$set": {"unread_for_ops": False}})
        ticket["unread_for_ops"] = False
    events = await support_ticket_events.find({"ticket_id": ticket_id}).sort("created_at", 1).to_list(200)
    await mark_ticket_read(staff["id"], ticket_id)
    context = await support_ticket_context.find_one({"ticket_id": ticket_id}, {"_id": 0})
    total = len(ticket.get("messages", []))
    end = total if before is None else max(0, min(before, total))
    start = max(0, end - limit)
    ticket = {**ticket, "messages": ticket.get("messages", [])[start:end]}
    return {
        "ticket": SupportTicket(**ticket).model_dump(mode="json"),
        "events": [SupportTicketEvent(**e).model_dump(mode="json") for e in events],
        "context": context,
        "diagnosis": await support_ai.diagnose(ticket, (context or {}).get("snapshot")),
        "message_total": total,
        "has_older": start > 0,
        "first_index": start,
    }


@router.post("/tickets/{ticket_id}/claim")
@limiter.limit("30/minute")
async def claim_ticket(request: Request, ticket_id: str, staff: dict = Depends(require_platform_staff)) -> dict:
    """Atomic: the write only matches while nobody holds the ticket, so two
    staff clicking at once cannot both win."""
    now = datetime.now(timezone.utc)
    res = await support_tickets.update_one(
        {
            "id": ticket_id,
            "status": {"$ne": "closed"},
            "$or": [{"assignee_id": None}, {"assignee_id": {"$exists": False}}],
        },
        {"$set": {"assignee_id": staff["id"], "assignee_name": staff.get("name", ""), "updated_at": now}},
    )
    if res.matched_count == 0:
        ticket = await _get_ticket(ticket_id)  # 404 if it doesn't exist
        if ticket["status"] == "closed":
            raise HTTPException(status_code=409, detail="This ticket is closed.")
        holder = ticket.get("assignee_name") or "another agent"
        raise HTTPException(status_code=409, detail=f"{holder} already has this ticket.")

    # Claiming an untouched ticket starts the work on it.
    await support_tickets.update_one(
        {"id": ticket_id, "status": "open"},
        {"$set": {"status": "investigating", "updated_at": now}},
    )
    await log_event(ticket_id, type="claimed", **_actor(staff))
    return {"ticket": SupportTicket(**await _get_ticket(ticket_id)).model_dump(mode="json")}


def _clean_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for t in tags:
        t = t.strip().lower()[:30]
        if t and t not in seen:
            seen.append(t)
    return seen


async def _patch_ticket(ticket_id: str, body: SupportTicketStaffUpdate, staff: dict) -> dict:
    """The one place a staff edit is applied (the route and bulk actions both use it)."""
    ticket = await _get_ticket(ticket_id)
    now = datetime.now(timezone.utc)
    sets: dict = {"updated_at": now}
    events: list[tuple[str, dict]] = []

    if body.status is not None and body.status.value != ticket["status"]:
        target = body.status.value
        if target == "closed" and ticket["status"] != "resolved":
            raise HTTPException(status_code=409, detail="Only a resolved ticket can be closed by staff.")
        sets.update(transition_fields(ticket["status"], target, now))
        events.append(("status_changed", {"from": ticket["status"], "to": target}))

    if body.severity is not None and body.severity.value != ticket["severity"]:
        sets["severity"] = body.severity.value
        events.append(("priority_changed", {"from": ticket["severity"], "to": body.severity.value}))
        # New priority, new targets. What already happened (first reply, breaches) is kept.
        sets["sla"] = sla.compute(
            ticket["created_at"], body.severity.value, ticket.get("workspace_tier"),
            await sla.get_config(), ticket.get("sla"),
        )

    if body.assignee_id is not None:
        if ticket["status"] == "closed":
            raise HTTPException(status_code=409, detail="A closed ticket can't be assigned.")
        new_id = body.assignee_id.strip()
        if new_id == "":
            if ticket.get("assignee_id") and ticket["assignee_id"] != staff["id"] and not has_role_at_least(staff, "lead"):
                raise HTTPException(status_code=403, detail="Only a lead can unassign someone else's ticket.")
            sets.update({"assignee_id": None, "assignee_name": None})
            events.append(("assigned", {"to": None}))
        elif new_id != ticket.get("assignee_id"):
            if new_id != staff["id"] and not has_role_at_least(staff, "lead"):
                raise HTTPException(status_code=403, detail="Only a lead can assign a ticket to someone else.")
            target_user = await users.find_one({"id": new_id})
            if not target_user or not (target_user.get("is_platform_staff") or target_user.get("is_master_admin")):
                raise HTTPException(status_code=400, detail="That person isn't on the support team.")
            sets.update({"assignee_id": new_id, "assignee_name": target_user.get("name", "")})
            events.append(("assigned", {"to": new_id, "to_name": target_user.get("name", "")}))

    if body.clear_snooze:
        if ticket.get("snoozed_until"):
            sets["snoozed_until"] = None
            events.append(("unsnoozed", {}))
    elif body.snoozed_until is not None:
        until = body.snoozed_until if body.snoozed_until.tzinfo else body.snoozed_until.replace(tzinfo=timezone.utc)
        if until <= now:
            raise HTTPException(status_code=400, detail="Pick a time in the future.")
        sets["snoozed_until"] = until
        events.append(("snoozed", {"until": until.isoformat()}))

    if body.tags is not None:
        cleaned = _clean_tags(body.tags)
        if cleaned != ticket.get("tags", []):
            sets["tags"] = cleaned
            events.append(("tags_changed", {"tags": cleaned}))

    if events:
        # A status change the member should hear about.
        if any(t == "status_changed" for t, _ in events):
            sets["unread_for_member"] = True
        await support_tickets.update_one({"id": ticket_id}, {"$set": sets})
        for etype, data in events:
            await log_event(ticket_id, type=etype, data=data, **_actor(staff))
        for etype, data in events:
            if etype == "status_changed" and data.get("to") == "resolved":
                await notify_member(
                    ticket, "ticket_resolved", "Your ticket is resolved",
                    "If it is not fixed, reply on the ticket and we will pick it straight back up.",
                )
    return {"ticket": SupportTicket(**await _get_ticket(ticket_id)).model_dump(mode="json")}


@router.patch("/tickets/{ticket_id}")
@limiter.limit("60/minute")
async def update_ticket(
    request: Request,
    ticket_id: str,
    body: SupportTicketStaffUpdate,
    staff: dict = Depends(require_platform_staff),
) -> dict:
    return await _patch_ticket(ticket_id, body, staff)


@router.patch("/tickets/{ticket_id}/status")
@limiter.limit("30/minute")
async def update_ticket_status(
    request: Request,
    ticket_id: str,
    body: SupportTicketStatusUpdate,
    staff: dict = Depends(require_platform_staff),
) -> dict:
    """Kept so existing callers keep working; same rules as the general PATCH."""
    return await _patch_ticket(ticket_id, SupportTicketStaffUpdate(status=body.status), staff)


async def _post_staff_message(ticket_id: str, body: SupportStaffMessageCreate, staff: dict) -> dict:
    """The one place a staff reply or note is written (the route and bulk replies both use it)."""
    ticket = await _get_ticket(ticket_id)
    now = datetime.now(timezone.utc)

    if ticket["status"] == "closed" and not body.is_internal:
        raise HTTPException(status_code=409, detail="This ticket is closed, so it can't be replied to.")
    await _flag_deleted([ticket])
    if ticket["created_by_deleted"] and not body.is_internal:
        raise HTTPException(status_code=409, detail="This member no longer has an account, so there is no one to reply to. You can still add an internal note.")

    # Someone else replied while this page was open: say so before sending on top of it.
    total_now = len(ticket.get("messages", []))
    if body.known_message_total is not None and total_now > body.known_message_total and not body.force:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "newer_reply",
                "message": "A new message arrived on this ticket after you opened it. Read it first, or send anyway.",
                "message_total": total_now,
            },
        )

    try:
        attachments = await files_service.claim(body.attachment_ids, uploader_id=staff["id"], ticket_id=ticket_id)
    except files_service.AttachmentRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    message = SupportMessage(
        sender="staff",
        sender_name=staff.get("name", "Recast Support"),
        text=body.text,
        created_at=now,
        is_internal=body.is_internal,
        attachments=attachments,
    )
    sets: dict = {"updated_at": now}
    status_now = ticket["status"]

    if not body.is_internal:
        sets["last_staff_message_at"] = now
        sets["unread_for_member"] = True
        if ticket.get("sla") and not ticket["sla"].get("first_responded_at"):
            sets["sla.first_responded_at"] = now
        # Replying takes the ticket if nobody has it.
        if not ticket.get("assignee_id"):
            sets["assignee_id"] = staff["id"]
            sets["assignee_name"] = staff.get("name", "")
        if status_now == "open":
            sets.update(transition_fields("open", "investigating", now))
            status_now = "investigating"

    if body.set_status is not None and body.set_status.value != status_now:
        target = body.set_status.value
        if target == "closed" and status_now != "resolved":
            raise HTTPException(status_code=409, detail="Only a resolved ticket can be closed by staff.")
        sets.update(transition_fields(status_now, target, now))
        sets["unread_for_member"] = sets.get("unread_for_member", False) or not body.is_internal

    await support_tickets.update_one(
        {"id": ticket_id}, {"$push": {"messages": message.model_dump()}, "$set": sets}
    )
    await log_event(
        ticket_id,
        type="note_added" if body.is_internal else "replied",
        data={"set_status": body.set_status.value} if body.set_status else {},
        **_actor(staff),
    )
    if not body.is_internal:
        if body.set_status is not None and body.set_status.value == "resolved":
            await notify_member(ticket, "ticket_resolved", "Your ticket is resolved", body.text)
        else:
            await notify_member(
                ticket, "staff_reply", f"{staff.get('name') or 'Recast Support'} replied", body.text
            )
    return {"ticket": SupportTicket(**await _get_ticket(ticket_id)).model_dump(mode="json")}
