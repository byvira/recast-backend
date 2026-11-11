"""Adding a member's reply to a ticket. One function, used by the Support page and by a reply sent from an email program, so the
two can never behave differently (reopening a resolved ticket, telling the person working on it, the audit row)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db.mongo import support_ticket_events, support_tickets
from app.models.support import SupportMessage
from app.shared import support_rules as rules
from app.shared.support import log_event, transition_fields
from app.shared.support_notify import notify_staff_user


class ReplyConflict(Exception):
    """The ticket changed while the reply was being written."""


async def member_hit_message_limit(user_id: str) -> bool:
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    sent = await support_ticket_events.count_documents(
        {"actor_id": user_id, "actor_type": "member", "type": {"$in": ["replied", "reopened"]}, "created_at": {"$gte": since}}
    )
    return sent >= rules.MAX_MESSAGES_PER_MEMBER_PER_HOUR


async def add_member_message(
    ticket: dict, *, user: dict, text: str, attachments: Optional[list[dict]] = None, via: Optional[str] = None
) -> None:
    """Write the reply. Raises ReplyConflict when the ticket's status changed under it. The caller has already checked who may reply,
    that the ticket is not closed and the hourly limit."""
    ticket_id = ticket["id"]
    now = datetime.now(timezone.utc)
    message = SupportMessage(
        sender="member", sender_name=user.get("name", ""), text=text, created_at=now,
        attachments=attachments or [], via=via,
    )
    set_fields: dict = {"updated_at": now, "last_member_message_at": now, "unread_for_ops": True}
    # A reply on a resolved ticket reopens it; a reply to a question moves it back to the person working on it.
    reopen_from = ticket["status"] if ticket["status"] in ("resolved", "waiting_on_member") else None
    if reopen_from:
        set_fields.update(transition_fields(reopen_from, "investigating", now))
        set_fields["reminder_sent_at"] = None
        set_fields["close_warning_sent_at"] = None
    written = await support_tickets.update_one(
        {"id": ticket_id, "status": ticket["status"]}, {"$push": {"messages": message.model_dump()}, "$set": set_fields}
    )
    if not written.matched_count:
        raise ReplyConflict()
    await log_event(
        ticket_id, actor_type="member", actor_id=user.get("id"), actor_name=user.get("name", ""),
        type="reopened" if reopen_from == "resolved" else "replied", data={"via": via} if via else None,
    )
    if ticket.get("assignee_id"):
        await notify_staff_user(
            ticket["assignee_id"], ticket, "member_replied", f"Reply on #{ticket.get('number') or ticket['id'][:6]}", text,
        )
