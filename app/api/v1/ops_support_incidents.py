"""Support tickets, incidents and escalation: one problem, many tickets.

* incidents group tickets about the same problem so staff fix it once and
  answer everyone at once
* escalating a ticket hands it to engineering and links it to an incident
* view-as-member shows a lead exactly what the member sees, with a stated
  reason, and leaves an audit record
* leads can stop a member filing more tickets (abuse), and admins set who owns
  each area so new tickets route themselves

Leads and admins only, except reading incidents.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.v1.ops_support import _actor, _get_ticket, _patch_ticket, _post_staff_message, _summary
from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import support_incidents, support_settings, support_tickets, users
from app.models.support import (
    SupportStaffMessageCreate,
    SupportTicket,
    SupportTicketStaffUpdate,
    SupportTicketStatus,
)
from app.shared.support import has_role_at_least, log_event, member_view, transition_fields
from app.shared.support_log import support_request_log
from app.shared.support_triage import get_routing

router = APIRouter(dependencies=[Depends(support_request_log)])
logger = logging.getLogger(__name__)

_OPEN = ["open", "investigating", "waiting_on_member", "waiting_on_engineering"]


def _need(staff: dict, role: str, message: str) -> None:
    if not has_role_at_least(staff, role):
        raise HTTPException(status_code=403, detail=message)


async def _get_incident(incident_id: str) -> dict:
    doc = await support_incidents.find_one({"id": incident_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Incident not found.")
    return doc


async def _out(doc: dict, *, with_tickets: bool = False) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["ticket_count"] = len(doc.get("ticket_ids", []))
    if with_tickets:
        rows = await support_tickets.find({"id": {"$in": doc.get("ticket_ids", [])}}).sort("created_at", 1).to_list(200)
        out["tickets"] = [_summary(r) for r in rows]
        out["open_ticket_count"] = sum(1 for r in rows if r["status"] in _OPEN)
    return out


# ── Incidents ────────────────────────────────────────────────────────────────
@router.get("/incidents")
@limiter.limit("60/minute")
async def list_incidents(
    request: Request,
    status: Optional[Literal["open", "monitoring", "resolved"]] = Query(default=None),
    staff: dict = Depends(require_platform_staff),
) -> dict:
    query: dict = {"status": status} if status else {}
    rows = await support_incidents.find(query).sort("created_at", -1).to_list(100)
    return {"incidents": [await _out(r) for r in rows]}


@router.get("/incidents/{incident_id}")
@limiter.limit("60/minute")
async def get_incident(request: Request, incident_id: str, staff: dict = Depends(require_platform_staff)) -> dict:
    return {"incident": await _out(await _get_incident(incident_id), with_tickets=True)}


class IncidentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    platform: Optional[str] = Field(default=None, max_length=40)
    ticket_ids: list[str] = Field(default_factory=list, max_length=100)
    eng_issue_url: Optional[str] = Field(default=None, max_length=300)


async def _attach(incident_id: str, ticket_ids: list[str], staff: dict) -> list[str]:
    """Link tickets to an incident (a ticket sits in at most one). Returns the ids that were linked."""
    linked: list[str] = []
    for tid in dict.fromkeys(ticket_ids):
        ticket = await support_tickets.find_one({"id": tid})
        if not ticket or ticket.get("incident_id") == incident_id:
            continue
        old = ticket.get("incident_id")
        if old:
            await support_incidents.update_one({"id": old}, {"$pull": {"ticket_ids": tid}})
        await support_tickets.update_one({"id": tid}, {"$set": {"incident_id": incident_id}})
        await support_incidents.update_one({"id": incident_id}, {"$addToSet": {"ticket_ids": tid}})
        await log_event(tid, type="incident_linked", data={"incident_id": incident_id}, **_actor(staff))
        linked.append(tid)
    return linked


@router.post("/incidents")
@limiter.limit("20/minute")
async def create_incident(
    request: Request, body: IncidentCreate, staff: dict = Depends(require_platform_staff)
) -> dict:
    _need(staff, "lead", "Creating an incident needs a lead.")
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid4()),
        "title": body.title.strip(),
        "category": body.category,
        "platform": body.platform,
        "status": "open",
        "ticket_ids": [],
        "eng_issue_url": body.eng_issue_url,
        "created_by": staff["id"],
        "created_by_name": staff.get("name", ""),
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
    }
    await support_incidents.insert_one(doc)
    await _attach(doc["id"], body.ticket_ids, staff)
    return {"incident": await _out(await _get_incident(doc["id"]), with_tickets=True)}


class IncidentUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    status: Optional[Literal["open", "monitoring", "resolved"]] = None
    eng_issue_url: Optional[str] = Field(default=None, max_length=300)
    # Once fixed: what happens to tickets that were waiting on engineering.
    after_fix_status: Optional[Literal["investigating", "resolved"]] = None
    after_fix_message: Optional[str] = Field(default=None, max_length=5000)
    add_ticket_ids: list[str] = Field(default_factory=list, max_length=100)
    remove_ticket_ids: list[str] = Field(default_factory=list, max_length=100)


@router.patch("/incidents/{incident_id}")
@limiter.limit("30/minute")
async def update_incident(
    request: Request, incident_id: str, body: IncidentUpdate, staff: dict = Depends(require_platform_staff)
) -> dict:
    _need(staff, "lead", "Changing an incident needs a lead.")
    incident = await _get_incident(incident_id)
    now = datetime.now(timezone.utc)
    sets: dict[str, Any] = {"updated_at": now}
    if body.title is not None:
        sets["title"] = body.title.strip()
    if body.eng_issue_url is not None:
        sets["eng_issue_url"] = body.eng_issue_url or None
    if body.add_ticket_ids:
        await _attach(incident_id, body.add_ticket_ids, staff)
    for tid in body.remove_ticket_ids:
        await support_incidents.update_one({"id": incident_id}, {"$pull": {"ticket_ids": tid}})
        await support_tickets.update_one({"id": tid, "incident_id": incident_id}, {"$set": {"incident_id": None}})

    fixed_now = body.status == "resolved" and incident["status"] != "resolved"
    if body.status is not None:
        sets["status"] = body.status
        sets["resolved_at"] = now if body.status == "resolved" else None
    await support_incidents.update_one({"id": incident_id}, {"$set": sets})

    resolved_tickets: list[str] = []
    if fixed_now:
        # Tell every ticket that was waiting on the fix, and move it on as the staff chose.
        waiting = await support_tickets.find(
            {"incident_id": incident_id, "status": "waiting_on_engineering"}
        ).to_list(200)
        for t in waiting:
            await log_event(t["id"], type="incident_resolved", data={"incident_id": incident_id}, **_actor(staff))
            try:
                if body.after_fix_message:
                    await _post_staff_message(
                        t["id"],
                        SupportStaffMessageCreate(
                            text=body.after_fix_message,
                            set_status=SupportTicketStatus(body.after_fix_status) if body.after_fix_status else None,
                        ),
                        staff,
                    )
                elif body.after_fix_status:
                    await _patch_ticket(t["id"], SupportTicketStaffUpdate(status=SupportTicketStatus(body.after_fix_status)), staff)
                resolved_tickets.append(t["id"])
            except HTTPException:
                logger.warning("Couldn't update ticket %s after incident %s was fixed", t["id"], incident_id)
    out = await _out(await _get_incident(incident_id), with_tickets=True)
    out["notified_ticket_ids"] = resolved_tickets
    return {"incident": out}


class IncidentReply(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    set_status: Optional[SupportTicketStatus] = None


@router.post("/incidents/{incident_id}/reply")
@limiter.limit("10/minute")
async def reply_to_incident(
    request: Request, incident_id: str, body: IncidentReply, staff: dict = Depends(require_platform_staff)
) -> dict:
    """One message to every open ticket in the incident."""
    _need(staff, "lead", "Replying to a whole incident needs a lead.")
    incident = await _get_incident(incident_id)
    done: list[str] = []
    failed: list[dict] = []
    for tid in incident.get("ticket_ids", []):
        ticket = await support_tickets.find_one({"id": tid})
        if not ticket or ticket["status"] in ("closed",):
            continue
        try:
            await _post_staff_message(
                tid, SupportStaffMessageCreate(text=body.text, set_status=body.set_status), staff
            )
            done.append(tid)
        except HTTPException as exc:
            failed.append({"id": tid, "reason": exc.detail if isinstance(exc.detail, str) else "Not allowed."})
    return {"done": done, "failed": failed}


# ── Escalate to engineering ──────────────────────────────────────────────────
class EscalateBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    notes: str = Field(min_length=1, max_length=5000)
    eng_issue_url: Optional[str] = Field(default=None, max_length=300)
    # Attach to an incident that already exists instead of opening a new one.
    incident_id: Optional[str] = Field(default=None, max_length=80)


@router.post("/tickets/{ticket_id}/escalate")
@limiter.limit("20/minute")
async def escalate(
    request: Request, ticket_id: str, body: EscalateBody, staff: dict = Depends(require_platform_staff)
) -> dict:
    ticket = await _get_ticket(ticket_id)
    if ticket["status"] in ("closed", "resolved", "waiting_on_engineering"):
        raise HTTPException(status_code=409, detail="This ticket can't be escalated in its current state.")
    now = datetime.now(timezone.utc)

    if body.incident_id:
        incident = await _get_incident(body.incident_id)
        if body.eng_issue_url:
            await support_incidents.update_one({"id": incident["id"]}, {"$set": {"eng_issue_url": body.eng_issue_url}})
    else:
        incident = {
            "id": str(uuid4()),
            "title": body.title.strip(),
            "category": ticket.get("category", ""),
            "platform": (ticket.get("source_context") or {}).get("id")
            if (ticket.get("source_context") or {}).get("type") == "platform" else None,
            "status": "open",
            "ticket_ids": [],
            "eng_issue_url": body.eng_issue_url,
            "created_by": staff["id"],
            "created_by_name": staff.get("name", ""),
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
        }
        await support_incidents.insert_one(incident)
    await _attach(incident["id"], [ticket_id], staff)

    # Escalating is taking the ticket: an untouched one is taken first, so the status move is a legal one.
    if not ticket.get("assignee_id"):
        await _patch_ticket(ticket_id, SupportTicketStaffUpdate(assignee_id=staff["id"]), staff)
    if ticket["status"] == "open":
        await _patch_ticket(ticket_id, SupportTicketStaffUpdate(status=SupportTicketStatus.INVESTIGATING), staff)

    # Through the same message code as any reply: takes the ticket, applies the transition.
    note = f"Escalated to engineering: {body.title.strip()}\n\n{body.notes}"
    await _post_staff_message(
        ticket_id,
        SupportStaffMessageCreate(text=note, is_internal=True, set_status=SupportTicketStatus.WAITING_ON_ENGINEERING),
        staff,
    )
    await log_event(ticket_id, type="escalated", data={"incident_id": incident["id"], "title": body.title}, **_actor(staff))
    return {
        "ticket": SupportTicket(**await _get_ticket(ticket_id)).model_dump(mode="json"),
        "incident": await _out(await _get_incident(incident["id"])),
    }


# ── View as member ───────────────────────────────────────────────────────────
class ViewAsBody(BaseModel):
    reason: str = Field(min_length=10, max_length=300)


@router.post("/tickets/{ticket_id}/view-as")
@limiter.limit("20/minute")
async def view_as_member(
    request: Request, ticket_id: str, body: ViewAsBody, staff: dict = Depends(require_platform_staff)
) -> dict:
    """Read-only: returns exactly what the member sees of this ticket. Needs a
    lead and a reason, and every use is recorded."""
    _need(staff, "lead", "Viewing as a member needs a lead.")
    ticket = await _get_ticket(ticket_id)
    await log_event(
        ticket_id, type="viewed_as_member",
        data={"reason": body.reason.strip(), "member_id": ticket["created_by"]}, **_actor(staff),
    )
    return {"as_member": member_view(ticket, viewer_id=ticket["created_by"]).model_dump(mode="json")}


# ── Mute a member (abuse) ────────────────────────────────────────────────────
class MuteBody(BaseModel):
    muted: bool


@router.post("/tickets/{ticket_id}/mute-member")
@limiter.limit("20/minute")
async def mute_member(
    request: Request, ticket_id: str, body: MuteBody, staff: dict = Depends(require_platform_staff)
) -> dict:
    """Stop (or allow again) this member filing new tickets. Their existing
    tickets stay as they are."""
    _need(staff, "lead", "Muting a member needs a lead.")
    ticket = await _get_ticket(ticket_id)
    res = await users.update_one({"id": ticket["created_by"]}, {"$set": {"support_muted": body.muted}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="That member no longer exists.")
    await log_event(ticket_id, type="member_muted" if body.muted else "member_unmuted", **_actor(staff))
    return {"muted": body.muted}


# ── Who owns which area ──────────────────────────────────────────────────────
class RoutingBody(BaseModel):
    # area (ticket category) -> staff user id; an empty id clears the area.
    owners: dict[str, str]


@router.get("/routing")
@limiter.limit("30/minute")
async def read_routing(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    return {"owners": await get_routing(), "can_edit": has_role_at_least(staff, "admin")}


@router.put("/routing")
@limiter.limit("10/minute")
async def write_routing(request: Request, body: RoutingBody, staff: dict = Depends(require_platform_staff)) -> dict:
    _need(staff, "admin", "Only an admin can set who owns each area.")
    owners: dict[str, str] = {}
    for area, uid in body.owners.items():
        if not uid:
            continue
        person = await users.find_one({"id": uid})
        if not person or not (person.get("is_platform_staff") or person.get("is_master_admin")):
            raise HTTPException(status_code=400, detail=f"{area}: that person isn't on the support team.")
        owners[area] = uid
    await support_settings.update_one(
        {"_id": "routing"},
        {"$set": {"owners": owners, "updated_by": staff["id"], "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return {"owners": owners}
