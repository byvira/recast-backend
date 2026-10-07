"""Staff view of waitlist leads and contact messages. Cross-tenant, behind the same platform-staff gate as the rest of Ops.

Waitlist: counts, a filtered list, one lead with its history, set a status by hand (joined, reviewed, hold), add a note, export a CSV.
Contact: a filtered inbox (team-size messages first), one message with its thread, take it, change its status, add a note, reply.
Every change writes a line to the lead audit trail (`lead_events`).
"""

import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.db.mongo import contact_messages, lead_events, waitlist_leads
from app.models.leads import SALES_TOPIC, ContactReply, ContactUpdate, StaffEmail, WaitlistUpdate
from app.shared.leads import record_event

logger = logging.getLogger(__name__)
router = APIRouter()

REPLY_TEMPLATE = "contact-reply"
# A general template for a staff-written email: the subject line and the body are filled in from what the staff member typed.
STAFF_TEMPLATE = "ops-message"
# A message nobody has answered after this long is shown as overdue.
OVERDUE_AFTER = timedelta(days=3)
EXPORT_LIMIT = 5000
_NO_ID = {"_id": 0}


def _joined_filter() -> dict[str, Any]:
    """Leads from before statuses existed have no status field: they count as joined."""
    return {"$or": [{"status": "joined"}, {"status": {"$exists": False}}]}


def _waitlist_query(status: Optional[str], q: Optional[str], role: Optional[str], team_size: Optional[str], source: Optional[str]) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = []
    if status == "joined":
        clauses.append(_joined_filter())
    elif status:
        clauses.append({"status": status})
    if q:
        clauses.append({"email": {"$regex": re.escape(q.strip().lower())}})
    for field, value in (("role", role), ("team_size", team_size), ("source", source)):
        if value:
            clauses.append({field: value})
    return {"$and": clauses} if clauses else {}


def _lead_row(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["id"],
        "email": doc["email"],
        "status": doc.get("status", "joined"),
        "source": doc.get("source", "unknown"),
        "role": doc.get("role"),
        "team_size": doc.get("team_size"),
        "platforms": doc.get("platforms") or [],
        "referral_count": doc.get("referral_count", 0),
        "note_count": len(doc.get("notes") or []),
        "created_at": doc.get("created_at"),
    }


def _is_overdue(doc: dict[str, Any], now: datetime) -> bool:
    created = doc.get("created_at")
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return doc.get("status") in ("new", "open") and created is not None and now - created > OVERDUE_AFTER


def _message_row(doc: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "id": doc["id"],
        "reference": doc.get("reference"),
        "name": doc["name"],
        "email": doc["email"],
        "topic": doc.get("topic", "general"),
        "status": doc.get("status", "new"),
        "assignee": doc.get("assignee"),
        "is_sales": doc.get("topic") == SALES_TOPIC,
        "overdue": _is_overdue(doc, now),
        "preview": doc["message"][:140],
        "created_at": doc.get("created_at"),
    }


async def _events(target_id: str) -> list[dict[str, Any]]:
    return await lead_events.find({"target_id": target_id}, _NO_ID).sort("at", -1).limit(50).to_list(50)


# ── Overview ────────────────────────────────────────────────────────────────

@router.get("/overview")
@limiter.limit("60/minute")
async def overview(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    by_status = {row["_id"]: row["count"] async for row in waitlist_leads.aggregate([{"$group": {"_id": {"$ifNull": ["$status", "joined"]}, "count": {"$sum": 1}}}])}
    waitlist_total = sum(by_status.values())

    async def top(field: str, limit: int) -> list[dict[str, Any]]:
        rows = waitlist_leads.aggregate([{"$group": {"_id": {"$ifNull": [f"${field}", "unknown"]}, "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": limit}])
        return [{"value": row["_id"], "count": row["count"]} async for row in rows]

    invited_ever = by_status.get("invited", 0) + by_status.get("activated", 0) + by_status.get("expired", 0)
    contact_by_status = {row["_id"]: row["count"] async for row in contact_messages.aggregate([{"$group": {"_id": {"$ifNull": ["$status", "new"]}, "count": {"$sum": 1}}}])}
    open_sales = await contact_messages.count_documents({"topic": SALES_TOPIC, "status": {"$in": ["new", "open"]}})
    overdue = await contact_messages.count_documents({"status": {"$in": ["new", "open"]}, "created_at": {"$lt": now - OVERDUE_AFTER}})

    return {
        "waitlist": {
            "total": waitlist_total,
            "by_status": by_status,
            "new_this_week": await waitlist_leads.count_documents({"created_at": {"$gte": week_ago}}),
            "referrals": sum([row async for row in waitlist_leads.aggregate([{"$group": {"_id": None, "n": {"$sum": "$referral_count"}}}])] and [0]) or 0,
            "activation_rate": round(by_status.get("activated", 0) / invited_ever, 3) if invited_ever else None,
            "top_sources": await top("source", 5),
            "roles": await top("role", 6),
            "team_sizes": await top("team_size", 6),
        },
        "contact": {
            "total": sum(contact_by_status.values()),
            "by_status": contact_by_status,
            "new_this_week": await contact_messages.count_documents({"created_at": {"$gte": week_ago}}),
            "open_sales": open_sales,
            "overdue": overdue,
        },
    }


# ── Waitlist ────────────────────────────────────────────────────────────────

@router.get("/waitlist")
@limiter.limit("60/minute")
async def list_waitlist(
    request: Request,
    status: Optional[str] = Query(None, max_length=20),
    q: Optional[str] = Query(None, max_length=100),
    role: Optional[str] = Query(None, max_length=20),
    team_size: Optional[str] = Query(None, max_length=10),
    source: Optional[str] = Query(None, max_length=60),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    query = _waitlist_query(status, q, role, team_size, source)
    total = await waitlist_leads.count_documents(query)
    docs = await waitlist_leads.find(query, _NO_ID).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    return {"items": [_lead_row(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


def _csv_cell(value: Any) -> str:
    """A spreadsheet runs a cell that starts with = + - or @ as a formula, so those are made harmless."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/waitlist/export")
@limiter.limit("10/hour")
async def export_waitlist(
    request: Request,
    status: Optional[str] = Query(None, max_length=20),
    q: Optional[str] = Query(None, max_length=100),
    user: dict = Depends(require_platform_staff),
) -> StreamingResponse:
    query = _waitlist_query(status, q, None, None, None)
    docs = await waitlist_leads.find(query, _NO_ID).sort("created_at", -1).limit(EXPORT_LIMIT).to_list(EXPORT_LIMIT)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["email", "status", "source", "role", "team_size", "platforms", "referrals", "joined"])
    for doc in docs:
        row = _lead_row(doc)
        writer.writerow([_csv_cell(v) for v in (row["email"], row["status"], row["source"], row["role"], row["team_size"], " ".join(row["platforms"]), row["referral_count"], row["created_at"])])
    await record_event("waitlist", "*", "export", user, f"{len(docs)} rows")
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="waitlist.csv"'})


@router.get("/waitlist/{lead_id}")
@limiter.limit("60/minute")
async def get_lead(request: Request, lead_id: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    doc = await waitlist_leads.find_one({"id": lead_id}, _NO_ID)
    if not doc:
        raise HTTPException(status_code=404, detail="That lead was not found.")
    referrer = None
    if doc.get("referred_by"):
        found = await waitlist_leads.find_one({"referral_code": doc["referred_by"]}, {"email": 1})
        referrer = found["email"] if found else None
    messages = await contact_messages.find({"email": doc["email"]}, _NO_ID).sort("created_at", -1).limit(10).to_list(10)
    now = datetime.now(timezone.utc)
    return {
        **_lead_row(doc),
        "utm": doc.get("utm") or {},
        "referred_by_email": referrer,
        "notes": doc.get("notes") or [],
        "emails": doc.get("emails") or [],
        "events": await _events(lead_id),
        "messages": [{"id": m["id"], "reference": m.get("reference"), "status": m.get("status", "new"), "created_at": m.get("created_at")} for m in messages],
        "now": now,
    }


@router.patch("/waitlist/{lead_id}")
@limiter.limit("60/minute")
async def update_lead(request: Request, lead_id: str, body: WaitlistUpdate, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    doc = await waitlist_leads.find_one({"id": lead_id}, {"status": 1, "email": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="That lead was not found.")
    current = doc.get("status", "joined")
    changes: dict[str, Any] = {}
    if body.status and body.status != current:
        if current in ("invited", "activated", "unsubscribed"):
            raise HTTPException(status_code=409, detail=f"A lead that is {current} cannot be changed by hand.")
        changes["status"] = body.status
        await record_event("waitlist", lead_id, "status", user, f"{current} to {body.status}")
    update: dict[str, Any] = {}
    if changes:
        update["$set"] = changes
    if body.note and body.note.strip():
        note = {"id": str(uuid4()), "text": body.note.strip(), "by": user.get("name") or user.get("email") or "Staff", "at": datetime.now(timezone.utc)}
        update["$push"] = {"notes": note}
        await record_event("waitlist", lead_id, "note", user, note["text"][:80])
    if update:
        await waitlist_leads.update_one({"id": lead_id}, update)
    fresh = await waitlist_leads.find_one({"id": lead_id}, _NO_ID)
    return _lead_row(fresh)


@router.post("/waitlist/{lead_id}/email")
@limiter.limit("30/hour")
async def email_lead(request: Request, lead_id: str, body: StaffEmail, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Write to someone on the waitlist. Only people already on the list can be written to here, and never someone who unsubscribed."""
    doc = await waitlist_leads.find_one({"id": lead_id}, _NO_ID)
    if not doc:
        raise HTTPException(status_code=404, detail="That lead was not found.")
    if doc.get("status") == "unsubscribed":
        raise HTTPException(status_code=409, detail="This person unsubscribed, so they cannot be emailed.")
    sent = False
    try:
        sent = await send_templated_email(STAFF_TEMPLATE, doc["email"], {"SUBJECT": body.subject, "MESSAGE": body.message, "SENDER": user.get("name") or "The Recast team"})
    except Exception:  # noqa: BLE001
        logger.warning("Staff email could not be sent", exc_info=True)
    if not sent:
        raise HTTPException(status_code=502, detail="The email could not be sent. Nothing was recorded. Please try again.")
    mail = {"id": str(uuid4()), "subject": body.subject, "text": body.message, "by": user.get("name") or user.get("email") or "Staff", "at": datetime.now(timezone.utc)}
    await waitlist_leads.update_one({"id": lead_id}, {"$push": {"emails": mail}})
    await record_event("waitlist", lead_id, "emailed", user, body.subject[:80])
    return {"ok": True}


# ── Contact ─────────────────────────────────────────────────────────────────

@router.get("/contact")
@limiter.limit("60/minute")
async def list_contact(
    request: Request,
    status: Optional[str] = Query(None, max_length=20),
    topic: Optional[str] = Query(None, max_length=40),
    q: Optional[str] = Query(None, max_length=100),
    mine: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = []
    if status:
        clauses.append({"status": status})
    else:
        clauses.append({"status": {"$ne": "spam"}})
    if topic:
        clauses.append({"topic": topic})
    if mine:
        clauses.append({"assignee.id": user["id"]})
    if q:
        pattern = re.escape(q.strip())
        clauses.append({"$or": [{"email": {"$regex": pattern, "$options": "i"}}, {"name": {"$regex": pattern, "$options": "i"}}, {"reference": {"$regex": pattern, "$options": "i"}}]})
    query = {"$and": clauses}
    total = await contact_messages.count_documents(query)
    # Team-size messages that still need an answer come first, then newest first.
    pipeline = [
        {"$match": query},
        {"$addFields": {"_rank": {"$cond": [{"$and": [{"$eq": ["$topic", SALES_TOPIC]}, {"$in": ["$status", ["new", "open"]]}]}, 0, 1]}}},
        {"$sort": {"_rank": 1, "created_at": -1}},
        {"$skip": (page - 1) * page_size},
        {"$limit": page_size},
        {"$project": {"_id": 0, "_rank": 0}},
    ]
    now = datetime.now(timezone.utc)
    docs = [doc async for doc in contact_messages.aggregate(pipeline)]
    return {"items": [_message_row(doc, now) for doc in docs], "total": total, "page": page, "page_size": page_size}


@router.get("/contact/{message_id}")
@limiter.limit("60/minute")
async def get_message(request: Request, message_id: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    doc = await contact_messages.find_one({"id": message_id}, _NO_ID)
    if not doc:
        raise HTTPException(status_code=404, detail="That message was not found.")
    now = datetime.now(timezone.utc)
    earlier = await contact_messages.find({"email": doc["email"], "id": {"$ne": message_id}}, _NO_ID).sort("created_at", -1).limit(10).to_list(10)
    lead = await waitlist_leads.find_one({"email": doc["email"]}, {"_id": 0, "id": 1, "status": 1})
    return {
        **_message_row(doc, now),
        "message": doc["message"],
        "internal_notes": doc.get("internal_notes") or [],
        "replies": doc.get("replies") or [],
        "answered_at": doc.get("answered_at"),
        "events": await _events(message_id),
        "earlier": [{"id": m["id"], "reference": m.get("reference"), "status": m.get("status", "new"), "created_at": m.get("created_at")} for m in earlier],
        "waitlist": {"id": lead["id"], "status": lead.get("status", "joined")} if lead else None,
    }


@router.patch("/contact/{message_id}")
@limiter.limit("60/minute")
async def update_message(request: Request, message_id: str, body: ContactUpdate, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    doc = await contact_messages.find_one({"id": message_id}, {"status": 1, "assignee": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="That message was not found.")
    sets: dict[str, Any] = {}
    push: dict[str, Any] = {}
    me = {"id": user["id"], "name": user.get("name") or user.get("email") or "Staff"}
    if body.status and body.status != doc.get("status", "new"):
        sets["status"] = body.status
        await record_event("contact", message_id, "status", user, f"{doc.get('status', 'new')} to {body.status}")
    if body.assign_to_me is True:
        sets["assignee"] = me
        if "status" not in sets and doc.get("status", "new") == "new":
            sets["status"] = "open"
        await record_event("contact", message_id, "assigned", user, me["name"])
    elif body.assign_to_me is False:
        sets["assignee"] = None
        await record_event("contact", message_id, "unassigned", user)
    if body.note and body.note.strip():
        note = {"id": str(uuid4()), "text": body.note.strip(), "by": me["name"], "at": datetime.now(timezone.utc)}
        push["internal_notes"] = note
        await record_event("contact", message_id, "note", user, note["text"][:80])
    update: dict[str, Any] = {}
    if sets:
        update["$set"] = sets
    if push:
        update["$push"] = push
    if update:
        await contact_messages.update_one({"id": message_id}, update)
    fresh = await contact_messages.find_one({"id": message_id}, _NO_ID)
    return _message_row(fresh, datetime.now(timezone.utc))


@router.post("/contact/{message_id}/reply")
@limiter.limit("30/hour")
async def reply_to_message(request: Request, message_id: str, body: ContactReply, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    doc = await contact_messages.find_one({"id": message_id}, _NO_ID)
    if not doc:
        raise HTTPException(status_code=404, detail="That message was not found.")
    if doc.get("status") == "spam":
        raise HTTPException(status_code=409, detail="A message marked as spam cannot be answered.")
    sent = False
    try:
        sent = await send_templated_email(REPLY_TEMPLATE, doc["email"], {"NAME": doc["name"], "REFERENCE": doc.get("reference", ""), "MESSAGE": body.message})
    except Exception:  # noqa: BLE001
        logger.warning("Contact reply could not be sent", exc_info=True)
    if not sent:
        raise HTTPException(status_code=502, detail="The reply could not be sent. Nothing was recorded. Please try again.")
    now = datetime.now(timezone.utc)
    reply = {"id": str(uuid4()), "text": body.message, "by": user.get("name") or user.get("email") or "Staff", "at": now}
    await contact_messages.update_one({"id": message_id}, {"$push": {"replies": reply}, "$set": {"status": "answered", "answered_at": now}})
    await record_event("contact", message_id, "replied", user, body.message[:80])
    return {"ok": True, "status": "answered"}
