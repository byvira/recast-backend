"""Support data retention and deletion.

Two ways a ticket loses its words, and one thing that never goes:

* **Retention:** a ticket that has been closed for ``TICKET_RETENTION`` (about
  24 months) is cleaned by the housekeeping job.
* **Deletion request:** a member can ask to have their support data erased, or
  an admin can do it on their behalf (for example after an emailed request).
* **What stays:** the counts that make the metrics true: area, priority,
  status, timings, response targets and the thumbs up or down. Nothing that
  says what was written or who wrote it.

"Erased" means: message text, subject, names, email, attachments (also deleted
from storage), context snapshot, notifications, chats, tags, free-text
comments and device details. The audit trail keeps its shape (who did what and
when) but loses member names and free-text notes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.mongo import (
    support_chats,
    support_files,
    support_notifications,
    support_ticket_context,
    support_ticket_events,
    support_tickets,
)
from app.shared import storage
from app.shared import support_rules as rules
from app.shared.support import log_event, transition_fields

logger = logging.getLogger(__name__)

REMOVED_TEXT = "This message was removed."
DELETED_USER = "Deleted user"
_SYSTEM = {"actor_type": "system", "actor_id": None, "actor_name": "Recast"}


async def _delete_stored_files(docs: list[dict]) -> int:
    removed = 0
    for d in docs:
        try:
            await asyncio.to_thread(storage.delete_private_file, d["storage_key"])
            removed += 1
        except Exception:
            # The database record goes either way; a failed storage delete is logged so it can be retried by hand.
            logger.warning("Couldn't delete private file %s from storage", d.get("id"), exc_info=True)
    return removed


async def anonymize_ticket(ticket_id: str) -> bool:
    """Remove everything personal from one ticket, keeping only its counts.
    Returns False if it was already done or does not exist."""
    ticket = await support_tickets.find_one({"id": ticket_id})
    if not ticket or ticket.get("anonymized_at"):
        return False
    now = datetime.now(timezone.utc)

    messages = []
    for m in ticket.get("messages", []):
        messages.append(
            {
                **m,
                "text": REMOVED_TEXT,
                "attachments": [],
                "is_deleted": True,
                "sender_name": DELETED_USER if m.get("sender") == "member" else m.get("sender_name", ""),
            }
        )
    source = ticket.get("source_context") or None
    if source:
        source = {"type": source.get("type"), "id": source.get("id"), "route": None}

    rating = ticket.get("rating")
    if rating:
        rating = {**rating, "comment": None}

    await support_tickets.update_one(
        {"id": ticket_id},
        {
            "$set": {
                "subject": "Removed",
                "messages": messages,
                "created_by_name": DELETED_USER,
                "created_by_email": None,
                "client_env": None,
                "source_context": source,
                "tags": [],
                "rating": rating,
                "anonymized_at": now,
                "updated_at": now,
            }
        },
    )

    files = await support_files.find({"ticket_id": ticket_id}).to_list(200)
    await _delete_stored_files(files)
    await support_files.delete_many({"ticket_id": ticket_id})
    await support_ticket_context.delete_many({"ticket_id": ticket_id})
    await support_notifications.delete_many({"ticket_id": ticket_id})
    await support_ticket_events.update_many(
        {"ticket_id": ticket_id, "actor_type": "member"}, {"$set": {"actor_name": DELETED_USER}}
    )
    await support_ticket_events.update_many({"ticket_id": ticket_id}, {"$unset": {"data.note": ""}})
    return True


async def erase_member_data(user_id: str) -> dict:
    """Everything a member has in support, across every workspace: their
    tickets (active ones are closed first), chats, notifications and any
    uploaded files that never reached a ticket."""
    now = datetime.now(timezone.utc)
    tickets = await support_tickets.find({"created_by": user_id}).to_list(2000)
    erased = 0
    for t in tickets:
        if t["status"] != "closed":
            fields = transition_fields(t["status"], "closed", now)
            await support_tickets.update_one(
                {"id": t["id"]},
                {"$set": {**fields, "closed_reason": "data_erased", "updated_at": now}},
            )
        if await anonymize_ticket(t["id"]):
            erased += 1
            await log_event(t["id"], type="data_erased", **_SYSTEM)

    chats = await support_chats.delete_many({"user_id": user_id})
    await support_notifications.delete_many({"user_id": user_id})
    loose = await support_files.find({"uploaded_by": user_id}).to_list(500)
    await _delete_stored_files(loose)
    await support_files.delete_many({"uploaded_by": user_id})
    return {"tickets": erased, "chats": chats.deleted_count, "files": len(loose)}


_ATTACHMENT_KEYS = ("name", "filename", "size", "size_bytes", "content_type", "mime_type")


def _iso(value) -> Optional[str]:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


async def export_member_data(user_id: str) -> dict:
    """Everything a member has in support, as plain data they can keep: their tickets with the messages they could see,
    ratings and file names. Staff-only notes are never included, and neither are storage paths or anything about other members."""
    tickets = await support_tickets.find({"created_by": user_id}).sort("created_at", 1).to_list(2000)
    out_tickets = []
    for t in tickets:
        messages = []
        for m in t.get("messages", []):
            if m.get("is_internal"):
                continue
            messages.append({
                "from": m.get("sender"),
                "name": m.get("sender_name"),
                "text": m.get("text"),
                "sent_at": _iso(m.get("created_at")),
                "removed": bool(m.get("is_deleted")),
                "attachments": [{k: a.get(k) for k in _ATTACHMENT_KEYS if k in a} for a in (m.get("attachments") or [])],
            })
        out_tickets.append({
            "id": t.get("id"),
            "number": t.get("number"),
            "subject": t.get("subject"),
            "category": t.get("category"),
            "severity": t.get("severity"),
            "status": t.get("status"),
            "created_at": _iso(t.get("created_at")),
            "closed_at": _iso(t.get("closed_at")),
            "erased": bool(t.get("anonymized_at")),
            "rating": {"score": (t.get("rating") or {}).get("score"), "comment": (t.get("rating") or {}).get("comment")} if t.get("rating") else None,
            "messages": messages,
        })
    chats = await support_chats.count_documents({"user_id": user_id})
    return {"exported_at": datetime.now(timezone.utc).isoformat(), "tickets": out_tickets, "assistant_chats": chats}


def export_to_csv(data: dict) -> str:
    """One row per message, with the ticket's own details repeated, for a spreadsheet."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ticket_number", "ticket_id", "subject", "category", "status", "ticket_created_at", "from", "name", "sent_at", "text"])
    for t in data.get("tickets", []):
        base = [t.get("number"), t.get("id"), t.get("subject"), t.get("category"), t.get("status"), t.get("created_at")]
        if not t.get("messages"):
            writer.writerow([*base, "", "", "", ""])
        for m in t.get("messages", []):
            # A cell that starts with = + - or @ would run as a formula in a spreadsheet, so it is made plain text first.
            text = str(m.get("text") or "")
            if text[:1] in ("=", "+", "-", "@"):
                text = "'" + text
            writer.writerow([*base, m.get("from"), m.get("name"), m.get("sent_at"), text])
    return buffer.getvalue()


async def enforce_retention(now: Optional[datetime] = None) -> int:
    """Clean tickets that have been closed longer than the retention period."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - rules.TICKET_RETENTION
    old = await support_tickets.find(
        {"status": "closed", "closed_at": {"$lt": cutoff}, "anonymized_at": {"$exists": False}}
    ).to_list(rules.RETENTION_BATCH)
    cleaned = 0
    for t in old:
        if await anonymize_ticket(t["id"]):
            cleaned += 1
            await log_event(t["id"], type="retention_cleaned", **_SYSTEM)
    return cleaned
