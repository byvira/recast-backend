"""Support ticket notifications: an in-app row plus an email.

The in-app row is always written first and never depends on the email. The
email is sent in the background with a few retries, so a slow or failing mail
provider can neither slow a request down nor lose the in-app notification.

Email goes through the shared ``send_templated_email`` (Resend). It needs a
published Resend template with the alias ``support-ticket-update`` and these
variables: NAME, TICKET_NUMBER, SUBJECT, HEADLINE, MESSAGE, LINK. Until that
template exists in Resend, production sends fail safely (logged) and only the
in-app notification arrives.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.core.config import settings
from app.core.notifications import OPS_FROM, send_templated_email
from app.db.mongo import support_email_log, support_notifications, users

logger = logging.getLogger(__name__)

EMAIL_TEMPLATE = "support-ticket-update"
_EMAIL_ATTEMPTS = 3
_EMAIL_BACKOFF_SECONDS = (1.0, 3.0)
_MAX_OPS_RECIPIENTS = 50

# Tasks are held here so a fire-and-forget email is not garbage collected mid-send.
_background: set[asyncio.Task] = set()


def _ticket_label(ticket: dict) -> str:
    return f"#{ticket['number']}" if ticket.get("number") else f"#{str(ticket['id'])[:6]}"


def member_link(ticket: dict) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/dashboard/support/tickets/{ticket['id']}"


def ops_link(ticket: dict) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/ops/support/{ticket['id']}"


def preview(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _insert(user_id: str, ticket: dict, type: str, title: str, body: str, audience: str) -> None:
    await support_notifications.insert_one(
        {
            "id": str(uuid4()),
            "user_id": user_id,
            "audience": audience,  # "member" | "ops"
            "type": type,
            "ticket_id": ticket["id"],
            "title": title,
            "body": body,
            "read": False,
            "created_at": datetime.now(timezone.utc),
        }
    )


async def _send_email_with_retry(
    to: str, variables: dict, ticket_id: str, type: str,
    template: str = EMAIL_TEMPLATE, from_override: Optional[str] = None,
) -> None:
    for attempt in range(_EMAIL_ATTEMPTS):
        try:
            if await send_templated_email(template, to, variables, from_override):
                await _log_email(type, True)
                return
        except Exception:
            logger.warning("Support email attempt %d raised for ticket %s", attempt + 1, ticket_id, exc_info=True)
        if attempt < _EMAIL_ATTEMPTS - 1:
            await asyncio.sleep(_EMAIL_BACKOFF_SECONDS[attempt])
    await _log_email(type, False)
    # The in-app notification already went out; only the email is lost.
    logger.error("Support email (%s) for ticket %s failed after %d attempts", type, ticket_id, _EMAIL_ATTEMPTS)


async def _log_email(type: str, ok: bool) -> None:
    try:
        await support_email_log.insert_one({"at": datetime.now(timezone.utc), "type": type, "ok": ok})
    except Exception:
        logger.debug("Couldn't log an email attempt", exc_info=True)


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def notify_member(ticket: dict, type: str, headline: str, message: str, *, email: bool = True) -> None:
    """Tell the person who filed the ticket. Never raises."""
    try:
        member_id = ticket["created_by"]
        await _insert(member_id, ticket, type, headline, preview(message), "member")
        if not email:
            return
        user = await users.find_one({"id": member_id}, {"email": 1, "name": 1})
        if user and user.get("email"):
            spawn(
                _send_email_with_retry(
                    user["email"],
                    {
                        "NAME": user.get("name", ""),
                        "TICKET_NUMBER": _ticket_label(ticket),
                        "SUBJECT": ticket.get("subject", ""),
                        "HEADLINE": headline,
                        "MESSAGE": preview(message),
                        "LINK": member_link(ticket),
                    },
                    ticket["id"],
                    type,
                )
            )
    except Exception:
        logger.warning("Support member notification failed for ticket %s", ticket.get("id"), exc_info=True)


async def notify_staff_user(user_id: str, ticket: dict, type: str, title: str, body: str) -> None:
    try:
        await _insert(user_id, ticket, type, title, preview(body), "ops")
    except Exception:
        logger.warning("Support staff notification failed for ticket %s", ticket.get("id"), exc_info=True)


async def notify_ops_new_ticket(ticket: dict) -> None:
    """In-app only: every support team member sees a new ticket. Email for the
    unassigned backlog is a digest sent by the scheduler (Phase 4), not one
    email per ticket."""
    try:
        staff = await users.find(
            {"$or": [{"is_platform_staff": True}, {"is_master_admin": True}]}, {"id": 1}
        ).to_list(_MAX_OPS_RECIPIENTS)
        for s in staff:
            await _insert(
                s["id"], ticket, "new_ticket",
                f"New ticket {_ticket_label(ticket)}", preview(ticket.get("subject", "")), "ops",
            )
    except Exception:
        logger.warning("Support ops notification failed for ticket %s", ticket.get("id"), exc_info=True)


OPS_DIGEST_TEMPLATE = "support-ops-digest"


async def notify_leads(title: str, body: str, tickets: list[dict], *, type: str) -> None:
    """In-app for every lead and admin, plus one email each. The email needs a
    Resend template with alias ``support-ops-digest`` and variables COUNT, LIST
    and LINK; until it exists, only the in-app notification arrives."""
    try:
        leads = await users.find(
            {"$or": [{"is_master_admin": True}, {"support_role": {"$in": ["lead", "admin"]}}]},
            {"id": 1, "email": 1, "name": 1},
        ).to_list(_MAX_OPS_RECIPIENTS)
        if not tickets:
            return
        newline = chr(10)
        lines = newline.join(f"{_ticket_label(t)} {preview(t.get('subject', ''), 80)}" for t in tickets[:20])
        for lead in leads:
            await _insert(lead["id"], tickets[0], type, title, preview(body), "ops")
            if lead.get("email"):
                spawn(
                    _send_email_with_retry(
                        lead["email"],
                        {"COUNT": len(tickets), "LIST": lines, "LINK": f"{settings.FRONTEND_URL.rstrip('/')}/ops/support"},
                        tickets[0]["id"], type, OPS_DIGEST_TEMPLATE, OPS_FROM,
                    )
                )
    except Exception:
        logger.warning("Support lead notification failed", exc_info=True)


async def notify_leads_alert(key: str, title: str, body: str) -> None:
    """A health alert (not tied to one ticket) for every lead and admin, in the app."""
    try:
        leads = await users.find(
            {"$or": [{"is_master_admin": True}, {"support_role": {"$in": ["lead", "admin"]}}]}, {"id": 1}
        ).to_list(_MAX_OPS_RECIPIENTS)
        for lead in leads:
            await _insert(lead["id"], {"id": key, "subject": title}, "health_alert", title, preview(body), "ops")
    except Exception:
        logger.warning("Support health alert failed", exc_info=True)


async def unread_count(user_id: str, audience: str) -> int:
    return await support_notifications.count_documents({"user_id": user_id, "audience": audience, "read": False})


async def mark_ticket_read(user_id: str, ticket_id: str) -> None:
    await support_notifications.update_many(
        {"user_id": user_id, "ticket_id": ticket_id, "read": False}, {"$set": {"read": True}}
    )


def optional_str(value: Optional[str]) -> str:
    return value or ""
