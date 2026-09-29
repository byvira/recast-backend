"""Support tickets, staff speed tools: canned replies, who is viewing, bulk
actions, merging tickets, removing a message, and SLA targets.

Same staff gate as ``ops_support``; reassigning others, bulk work, merging and
canned replies shared with the team need a lead, removing a message and
changing SLA targets need an admin.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.v1.ops_support import _actor, _get_ticket, _patch_ticket, _post_staff_message
from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import (
    support_canned_replies,
    support_ticket_context,
    support_files,
    support_presence,
    support_settings,
    support_tickets,
)
from app.models.support import (
    SupportStaffMessageCreate,
    SupportTicket,
    SupportTicketSeverity,
    SupportTicketStaffUpdate,
    SupportTicketStatus,
)
from app.shared import storage
from app.shared import support_ai
from app.shared import support_metrics
from app.shared import support_privacy
from app.shared.support_log import support_request_log
from app.shared import support_rules as rules
from app.shared import support_sla as sla
from app.shared.support import has_role_at_least, log_event, staff_role, transition_fields
from app.shared.support_notify import notify_member

router = APIRouter(dependencies=[Depends(support_request_log)])
logger = logging.getLogger(__name__)


def _require_role(staff: dict, minimum: str, message: str) -> None:
    if not has_role_at_least(staff, minimum):
        raise HTTPException(status_code=403, detail=message)


# ── Canned replies ───────────────────────────────────────────────────────────
_VARIABLES = ("{name}", "{workspace}", "{ticket_number}")


class CannedReplyBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=rules.MAX_MESSAGE_CHARS)
    category: Optional[str] = Field(default=None, max_length=80)
    # Shared with the whole support team. Leads and admins only.
    shared: bool = False


def render_canned(text: str, ticket: dict) -> str:
    """Fill {name}, {workspace} and {ticket_number} from the ticket."""
    first_name = (ticket.get("created_by_name") or "").strip().split(" ")[0] or "there"
    number = f"#{ticket['number']}" if ticket.get("number") else f"#{str(ticket['id'])[:6]}"
    return (
        text.replace("{name}", first_name)
        .replace("{workspace}", ticket.get("workspace_name") or "your workspace")
        .replace("{ticket_number}", number)
    )


def _canned_out(doc: dict, staff: dict) -> dict:
    mine = doc.get("owner_id") == staff["id"]
    return {
        "id": doc["id"],
        "title": doc["title"],
        "body": doc["body"],
        "category": doc.get("category"),
        "shared": doc.get("owner_id") is None,
        "owner_name": doc.get("owner_name"),
        "can_edit": mine or has_role_at_least(staff, "lead"),
        "updated_at": doc.get("updated_at"),
    }


@router.get("/canned")
@limiter.limit("60/minute")
async def list_canned(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    rows = (
        await support_canned_replies.find({"$or": [{"owner_id": None}, {"owner_id": staff["id"]}]})
        .sort("title", 1)
        .to_list(300)
    )
    return {"replies": [_canned_out(r, staff) for r in rows], "variables": list(_VARIABLES)}


@router.post("/canned")
@limiter.limit("30/minute")
async def create_canned(
    request: Request, body: CannedReplyBody, staff: dict = Depends(require_platform_staff)
) -> dict:
    if body.shared:
        _require_role(staff, "lead", "Only a lead can share a reply with the whole team.")
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid4()),
        "title": body.title.strip(),
        "body": body.body,
        "category": body.category,
        "owner_id": None if body.shared else staff["id"],
        "owner_name": staff.get("name", ""),
        "created_at": now,
        "updated_at": now,
    }
    await support_canned_replies.insert_one(doc)
    return _canned_out(doc, staff)


async def _editable_canned(reply_id: str, staff: dict) -> dict:
    doc = await support_canned_replies.find_one({"id": reply_id})
    # A reply that is private to someone else does not exist as far as you can tell.
    if not doc or (doc.get("owner_id") not in (None, staff["id"]) and not has_role_at_least(staff, "lead")):
        raise HTTPException(status_code=404, detail="Reply not found.")
    if doc.get("owner_id") != staff["id"] and not has_role_at_least(staff, "lead"):
        raise HTTPException(status_code=403, detail="Only a lead can change a shared reply.")
    return doc


@router.put("/canned/{reply_id}")
@limiter.limit("30/minute")
async def update_canned(
    request: Request, reply_id: str, body: CannedReplyBody, staff: dict = Depends(require_platform_staff)
) -> dict:
    doc = await _editable_canned(reply_id, staff)
    if body.shared and doc.get("owner_id") is not None:
        _require_role(staff, "lead", "Only a lead can share a reply with the whole team.")
    owner_id = None if body.shared else (doc.get("owner_id") or staff["id"])
    fields = {
        "title": body.title.strip(),
        "body": body.body,
        "category": body.category,
        "owner_id": owner_id,
        "updated_at": datetime.now(timezone.utc),
    }
    await support_canned_replies.update_one({"id": reply_id}, {"$set": fields})
    return _canned_out({**doc, **fields}, staff)


@router.delete("/canned/{reply_id}")
@limiter.limit("30/minute")
async def delete_canned(request: Request, reply_id: str, staff: dict = Depends(require_platform_staff)) -> dict:
    await _editable_canned(reply_id, staff)
    await support_canned_replies.delete_one({"id": reply_id})
    return {"deleted": True}


@router.get("/canned/{reply_id}/render/{ticket_id}")
@limiter.limit("60/minute")
async def render_canned_reply(
    request: Request, reply_id: str, ticket_id: str, staff: dict = Depends(require_platform_staff)
) -> dict:
    doc = await support_canned_replies.find_one({"id": reply_id})
    if not doc or doc.get("owner_id") not in (None, staff["id"]):
        raise HTTPException(status_code=404, detail="Reply not found.")
    ticket = await _get_ticket(ticket_id)
    return {"text": render_canned(doc["body"], ticket)}


# ── Who is looking ───────────────────────────────────────────────────────────
_PRESENCE_WINDOW = timedelta(seconds=60)


@router.post("/presence/{ticket_id}")
@limiter.limit("120/minute")
async def presence(request: Request, ticket_id: str, staff: dict = Depends(require_platform_staff)) -> dict:
    """Heartbeat, sent every 30 seconds while a ticket is open. Returns the
    other people who have it open right now."""
    now = datetime.now(timezone.utc)
    await support_presence.update_one(
        {"ticket_id": ticket_id, "staff_id": staff["id"]},
        {"$set": {"name": staff.get("name", ""), "at": now}},
        upsert=True,
    )
    others = await support_presence.find(
        {"ticket_id": ticket_id, "staff_id": {"$ne": staff["id"]}, "at": {"$gte": now - _PRESENCE_WINDOW}}
    ).to_list(10)
    return {"viewers": [{"id": o["staff_id"], "name": o.get("name", "")} for o in others]}


# ── Bulk actions ─────────────────────────────────────────────────────────────
class BulkBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal["assign", "set_status", "add_tag", "canned_reply", "add_to_incident"]
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/tickets/bulk")
@limiter.limit("20/minute")
async def bulk(request: Request, body: BulkBody, staff: dict = Depends(require_platform_staff)) -> dict:
    """Apply one action to many tickets. Each ticket goes through the same
    single-ticket code as a manual edit, so the same rules and audit events
    apply, and one failure does not stop the rest."""
    _require_role(staff, "lead", "Bulk actions need a lead.")
    payload = body.payload
    canned: Optional[dict] = None
    if body.action == "canned_reply":
        canned = await support_canned_replies.find_one({"id": str(payload.get("canned_id", ""))})
        if not canned or canned.get("owner_id") not in (None, staff["id"]):
            raise HTTPException(status_code=404, detail="Reply not found.")

    if body.action == "add_to_incident":
        from app.api.v1.ops_support_incidents import _attach, _get_incident

        incident = await _get_incident(str(payload.get("incident_id", "")))
        linked = await _attach(incident["id"], body.ids, staff)
        return {"done": linked, "failed": [{"id": i, "reason": "Already in that incident, or not found."} for i in dict.fromkeys(body.ids) if i not in linked]}

    done: list[str] = []
    failed: list[dict] = []
    for tid in dict.fromkeys(body.ids):
        try:
            if body.action == "assign":
                await _patch_ticket(tid, SupportTicketStaffUpdate(assignee_id=str(payload.get("assignee_id", ""))), staff)
            elif body.action == "set_status":
                await _patch_ticket(tid, SupportTicketStaffUpdate(status=SupportTicketStatus(payload.get("status"))), staff)
            elif body.action == "add_tag":
                ticket = await _get_ticket(tid)
                tag = str(payload.get("tag", "")).strip()
                if not tag:
                    raise HTTPException(status_code=400, detail="Give the tag a name.")
                await _patch_ticket(tid, SupportTicketStaffUpdate(tags=[*ticket.get("tags", []), tag]), staff)
            elif body.action == "canned_reply":
                ticket = await _get_ticket(tid)
                set_status = payload.get("set_status")
                await _post_staff_message(
                    tid,
                    SupportStaffMessageCreate(
                        text=render_canned(canned["body"], ticket),
                        set_status=SupportTicketStatus(set_status) if set_status else None,
                    ),
                    staff,
                )
            done.append(tid)
        except HTTPException as exc:
            failed.append({"id": tid, "reason": exc.detail if isinstance(exc.detail, str) else "Not allowed."})
        except ValueError:
            failed.append({"id": tid, "reason": "That value isn't allowed."})
        except Exception:
            logger.warning("Bulk %s failed for ticket %s", body.action, tid, exc_info=True)
            failed.append({"id": tid, "reason": "Something went wrong."})
    return {"done": done, "failed": failed}


# ── Merge ────────────────────────────────────────────────────────────────────
class MergeBody(BaseModel):
    target_ticket_id: str = Field(min_length=1, max_length=80)


@router.post("/tickets/{ticket_id}/merge")
@limiter.limit("20/minute")
async def merge_ticket(
    request: Request, ticket_id: str, body: MergeBody, staff: dict = Depends(require_platform_staff)
) -> dict:
    """Fold a duplicate into another ticket from the same person: its messages
    and files move over, the duplicate closes with a link, and the member is
    told once."""
    _require_role(staff, "lead", "Merging tickets needs a lead.")
    if ticket_id == body.target_ticket_id:
        raise HTTPException(status_code=400, detail="A ticket can't be merged into itself.")
    source = await _get_ticket(ticket_id)
    target = await _get_ticket(body.target_ticket_id)
    if source["created_by"] != target["created_by"] or source["workspace_id"] != target["workspace_id"]:
        # Merging would show one person's messages to another.
        raise HTTPException(status_code=400, detail="Only tickets from the same person can be merged.")
    if source["status"] == "closed" or target["status"] == "closed":
        raise HTTPException(status_code=409, detail="A closed ticket can't be merged.")

    now = datetime.now(timezone.utc)

    def label(t: dict) -> str:
        return f"#{t['number']}" if t.get("number") else f"#{t['id'][:6]}"

    moved = [dict(m) for m in source.get("messages", [])]
    marker = {
        "sender": "system", "sender_name": "Recast", "is_internal": True, "created_at": now,
        "text": f"Merged from {label(source)} ({source.get('subject', '')}) by {staff.get('name', 'staff')}.",
        "attachments": [],
    }
    def _when(m: dict) -> datetime:
        at = m["created_at"]
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)

    combined = sorted([*target.get("messages", []), *moved, marker], key=_when)
    await support_tickets.update_one(
        {"id": target["id"]},
        {"$set": {"messages": combined, "updated_at": now, "unread_for_member": True, "unread_for_ops": True}},
    )
    await support_files.update_many({"ticket_id": source["id"]}, {"$set": {"ticket_id": target["id"]}})

    close_fields = transition_fields(source["status"], "closed", now)
    await support_tickets.update_one(
        {"id": source["id"]},
        {"$set": {**close_fields, "closed_reason": "merged", "merged_into": target["id"], "updated_at": now}},
    )
    await log_event(source["id"], type="merged", data={"into": target["id"], "into_number": target.get("number")}, **_actor(staff))
    await log_event(target["id"], type="merged_in", data={"from": source["id"], "from_number": source.get("number")}, **_actor(staff))
    await notify_member(
        {**source, "id": target["id"], "number": target.get("number")},
        "ticket_merged",
        "We combined your tickets",
        f"{label(source)} was a duplicate of {label(target)}, so we kept everything together on {label(target)}.",
    )
    return {"ticket": SupportTicket(**await _get_ticket(target["id"])).model_dump(mode="json")}


# ── Remove a message ─────────────────────────────────────────────────────────
@router.delete("/tickets/{ticket_id}/messages/{index}")
@limiter.limit("20/minute")
async def delete_message(
    request: Request, ticket_id: str, index: int, staff: dict = Depends(require_platform_staff)
) -> dict:
    """Admin only. The text and files are removed; a marker stays in the thread
    and the audit trail records who did it (never what it said)."""
    _require_role(staff, "admin", "Only an admin can remove a message.")
    ticket = await _get_ticket(ticket_id)
    messages = ticket.get("messages", [])
    if index < 0 or index >= len(messages) or messages[index].get("is_deleted"):
        raise HTTPException(status_code=404, detail="Message not found.")
    original = messages[index]
    file_ids = [a["file_id"] for a in original.get("attachments", [])]
    tombstone = {
        **original,
        "text": "This message was removed.",
        "attachments": [],
        "is_deleted": True,
    }
    now = datetime.now(timezone.utc)
    await support_tickets.update_one(
        {"id": ticket_id}, {"$set": {f"messages.{index}": tombstone, "updated_at": now}}
    )
    if file_ids:
        docs = await support_files.find({"id": {"$in": file_ids}}).to_list(len(file_ids))
        await support_files.update_many({"id": {"$in": file_ids}}, {"$set": {"ticket_id": None, "deleted": True}})
        for d in docs:
            try:
                await asyncio.to_thread(storage.delete_private_file, d["storage_key"])
            except Exception:
                logger.warning("Couldn't delete private file %s from storage", d.get("id"), exc_info=True)
    await log_event(
        ticket_id, type="message_deleted",
        data={"index": index, "sender": original.get("sender"), "was_internal": bool(original.get("is_internal"))},
        **_actor(staff),
    )
    return {"ticket": SupportTicket(**await _get_ticket(ticket_id)).model_dump(mode="json")}


# ── AI reply draft ───────────────────────────────────────────────────────────
@router.post("/tickets/{ticket_id}/ai-draft")
@limiter.limit("20/minute")
async def ai_draft(request: Request, ticket_id: str, staff: dict = Depends(require_platform_staff)) -> dict:
    """A suggested reply, as text only. It sends nothing and changes nothing;
    a person reads it, edits it and sends it."""
    ticket = await _get_ticket(ticket_id)
    context = await support_ticket_context.find_one({"ticket_id": ticket_id})
    return await support_ai.draft_reply(ticket, (context or {}).get("snapshot"), staff["id"])


# ── Metrics ──────────────────────────────────────────────────────────────────
@router.get("/metrics")
@limiter.limit("30/minute")
async def metrics(
    request: Request,
    days: int = 30,
    staff: dict = Depends(require_platform_staff),
) -> dict:
    days = max(1, min(days, 180))
    end = datetime.now(timezone.utc)
    data = await support_metrics.compute(end - timedelta(days=days), end)
    data["alerts"] = await support_metrics.active_alerts(end)
    return data


# ── Erase a member's support data (a deletion request) ──────────────────────
class EraseBody(BaseModel):
    confirm: bool
    # Why: who asked and how, e.g. "Requested by email on 3 March".
    reason: str = Field(min_length=10, max_length=300)


@router.post("/tickets/{ticket_id}/erase-member")
@limiter.limit("5/hour")
async def erase_member(
    request: Request, ticket_id: str, body: EraseBody, staff: dict = Depends(require_platform_staff)
) -> dict:
    """Admin only. Handles a member's deletion request: erases all their
    support data (see app.shared.support_privacy). The ticket this was done
    from keeps a record of who did it and why, but no words or names."""
    _require_role(staff, "admin", "Only an admin can erase a member's support data.")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Please confirm before erasing.")
    ticket = await _get_ticket(ticket_id)
    result = await support_privacy.erase_member_data(ticket["created_by"])
    await log_event(
        ticket_id, type="member_data_erased_by_staff",
        data={"reason": body.reason.strip(), **result}, **_actor(staff),
    )
    return result


# ── SLA targets ──────────────────────────────────────────────────────────────
class SlaBody(BaseModel):
    first_response_hours: dict[SupportTicketSeverity, float]
    resolution_hours: dict[SupportTicketSeverity, float]


@router.get("/sla")
@limiter.limit("30/minute")
async def get_sla(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    return {
        "config": await sla.get_config(),
        "defaults": sla.default_config(),
        "paid_plans_faster": bool(rules.PAID_TIERS),
        "can_edit": staff_role(staff) == "admin",
    }


@router.put("/sla")
@limiter.limit("10/minute")
async def update_sla(request: Request, body: SlaBody, staff: dict = Depends(require_platform_staff)) -> dict:
    _require_role(staff, "admin", "Only an admin can change the response targets.")
    fields: dict[str, dict] = {"first_response_hours": {}, "resolution_hours": {}}
    for key, source in (("first_response_hours", body.first_response_hours), ("resolution_hours", body.resolution_hours)):
        for prio, hours in source.items():
            if not 0 < hours <= 24 * 60:
                raise HTTPException(status_code=400, detail="Hours must be between 0 and 1,440.")
            fields[key][prio.value] = hours
    await support_settings.update_one(
        {"_id": "sla"}, {"$set": {**fields, "updated_by": staff["id"], "updated_at": datetime.now(timezone.utc)}}, upsert=True
    )
    return {"config": await sla.get_config()}
