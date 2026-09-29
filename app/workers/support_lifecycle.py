"""Support ticket housekeeping, run every few minutes.

* waiting on the member: a reminder after 3 days of silence, then the ticket is
  resolved after 7 (the member can still reply, which reopens it)
* resolved: a heads-up 2 days before it closes, then closed after 7 days
* snoozed: wakes up when its time comes and tells whoever holds it

All the timings live in ``app.shared.support_rules``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import support_tickets, users
from app.shared import support_rules as rules
from app.shared.support import log_event, transition_fields
from app.shared.support_metrics import active_alerts
from app.shared.support_privacy import enforce_retention
from app.shared.support_notify import notify_leads, notify_leads_alert, notify_member, notify_staff_user
from app.db.mongo import support_settings
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

_SYSTEM = {"actor_type": "system", "actor_id": None, "actor_name": "Recast"}


def _as_utc(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _label(ticket: dict) -> str:
    return f"#{ticket['number']}" if ticket.get("number") else f"#{ticket['id'][:6]}"


async def run_support_lifecycle(now: Optional[datetime] = None) -> dict:
    """One pass. Returns how many tickets each rule touched (used by tests)."""
    now = now or datetime.now(timezone.utc)
    done = {
        "reminded": 0, "auto_resolved": 0, "close_warned": 0, "auto_closed": 0, "woken": 0,
        "sla_breached": 0, "sla_digest": 0, "unassigned": 0, "alerts": 0, "retention": 0,
    }

    # ── waiting on the member ────────────────────────────────────────────────
    async for t in support_tickets.find({"status": "waiting_on_member"}):
        ref = _as_utc(t.get("last_staff_message_at")) or _as_utc(t.get("updated_at"))
        if ref is None:
            continue
        silent_for = now - ref
        if silent_for >= rules.WAITING_ON_MEMBER_AUTO_RESOLVE_AFTER:
            fields = transition_fields("waiting_on_member", "resolved", now)
            await support_tickets.update_one(
                {"id": t["id"], "status": "waiting_on_member"},
                {"$set": {**fields, "updated_at": now, "unread_for_member": True, "closed_reason": None}},
            )
            await log_event(t["id"], type="auto_resolved", data={"silent_days": silent_for.days}, **_SYSTEM)
            await notify_member(
                t, "ticket_resolved", "We resolved your ticket",
                "We had not heard back, so we marked it resolved. Reply on the ticket any time and we will pick it back up.",
            )
            done["auto_resolved"] += 1
        elif silent_for >= rules.WAITING_ON_MEMBER_REMINDER_AFTER and not t.get("reminder_sent_at"):
            await support_tickets.update_one({"id": t["id"]}, {"$set": {"reminder_sent_at": now}})
            await notify_member(
                t, "ticket_reminder", "We are still waiting to hear from you",
                f"Ticket {_label(t)} is waiting on your reply. If it is sorted, you can close it, otherwise tell us what you see.",
            )
            done["reminded"] += 1

    # ── resolved ─────────────────────────────────────────────────────────────
    async for t in support_tickets.find({"status": "resolved"}):
        resolved_at = _as_utc(t.get("resolved_at")) or _as_utc(t.get("updated_at"))
        if resolved_at is None:
            continue
        age = now - resolved_at
        if age >= rules.RESOLVED_AUTO_CLOSE_AFTER:
            fields = transition_fields("resolved", "closed", now)
            await support_tickets.update_one(
                {"id": t["id"], "status": "resolved"},
                {"$set": {**fields, "closed_reason": "auto_closed", "updated_at": now}},
            )
            await log_event(t["id"], type="auto_closed", **_SYSTEM)
            done["auto_closed"] += 1
        elif (
            age >= rules.RESOLVED_AUTO_CLOSE_AFTER - rules.AUTO_CLOSE_WARNING_BEFORE
            and not t.get("close_warning_sent_at")
        ):
            await support_tickets.update_one({"id": t["id"]}, {"$set": {"close_warning_sent_at": now}})
            days_left = max(1, (rules.RESOLVED_AUTO_CLOSE_AFTER - age).days + 1)
            await notify_member(
                t, "ticket_closing", "Your ticket will close soon",
                f"Ticket {_label(t)} closes in about {days_left} days. Reply if you still need help.",
            )
            done["close_warned"] += 1

    # ── snoozed ──────────────────────────────────────────────────────────────
    async for t in support_tickets.find({"snoozed_until": {"$lte": now, "$ne": None}}):
        await support_tickets.update_one(
            {"id": t["id"]}, {"$set": {"snoozed_until": None, "unread_for_ops": True, "updated_at": now}}
        )
        await log_event(t["id"], type="unsnoozed", **_SYSTEM)
        if t.get("assignee_id"):
            await notify_staff_user(
                t["assignee_id"], t, "snooze_over", f"Ticket {_label(t)} is back",
                t.get("subject", ""),
            )
        done["woken"] += 1

    # ── people who have left the support team ───────────────────────────────
    holders = await support_tickets.distinct("assignee_id", {"assignee_id": {"$ne": None}, "status": {"$nin": ["closed"]}})
    if holders:
        still = {
            u["id"] async for u in users.find(
                {"id": {"$in": holders}, "$or": [{"is_platform_staff": True}, {"is_master_admin": True}]}, {"id": 1}
            )
        }
        gone = [h for h in holders if h not in still]
        if gone:
            orphaned = await support_tickets.find(
                {"assignee_id": {"$in": gone}, "status": {"$nin": ["closed"]}}
            ).to_list(500)
            for t in orphaned:
                await support_tickets.update_one(
                    {"id": t["id"]}, {"$set": {"assignee_id": None, "assignee_name": None, "unread_for_ops": True, "updated_at": now}}
                )
                await log_event(t["id"], type="unassigned", data={"why": "assignee left the support team"}, **_SYSTEM)
            await notify_leads(
                f"{len(orphaned)} ticket{'s' if len(orphaned) != 1 else ''} lost their owner",
                "Their owner is no longer on the support team, so they are unassigned. Reassign them from the queue.",
                orphaned, type="tickets_orphaned",
            )
            done["unassigned"] = len(orphaned)

    # ── response targets ─────────────────────────────────────────────────────
    pending_digest: list[dict] = []
    async for t in support_tickets.find({"status": {"$nin": ["resolved", "closed"]}, "sla": {"$ne": None}}):
        s = t["sla"]
        first_due, resolve_due = _as_utc(s.get("first_response_due")), _as_utc(s.get("resolve_due"))
        breached: list[tuple[str, str]] = []
        if not s.get("first_responded_at") and first_due and now > first_due and not s.get("breached_first"):
            breached.append(("sla.breached_first", "No first reply yet"))
        if resolve_due and now > resolve_due and not s.get("breached_resolve"):
            breached.append(("sla.breached_resolve", "Not resolved in time"))
        for field, why in breached:
            await support_tickets.update_one({"id": t["id"]}, {"$set": {field: True}})
            await log_event(t["id"], type="sla_breached", data={"which": field.split(".")[1]}, **_SYSTEM)
            title = f"Ticket {_label(t)} is late"
            if t.get("assignee_id"):
                await notify_staff_user(t["assignee_id"], t, "sla_breached", title, why)
            else:
                await notify_leads(title, why, [t], type="sla_breached")
            done["sla_breached"] += 1

        # Unassigned and already past half of the time to a first reply.
        created = _as_utc(t.get("created_at"))
        if (
            not t.get("assignee_id") and not s.get("first_responded_at") and not s.get("digest_sent")
            and first_due and created and now >= created + (first_due - created) / 2
        ):
            pending_digest.append(t)
    if pending_digest:
        await notify_leads(
            f"{len(pending_digest)} unassigned ticket{'s' if len(pending_digest) != 1 else ''} running late",
            "Nobody has picked these up and half of the time for a first reply has gone.",
            pending_digest, type="sla_digest",
        )
        await support_tickets.update_many(
            {"id": {"$in": [t["id"] for t in pending_digest]}}, {"$set": {"sla.digest_sent": True}}
        )
        done["sla_digest"] = len(pending_digest)

    # ── retention: closed tickets past their keep-until date lose their words ─
    done["retention"] = await enforce_retention(now)

    # ── health alerts: each one at most once a day ───────────────────────────
    day = now.strftime("%Y-%m-%d")
    for alert in await active_alerts(now):
        try:
            await support_settings.insert_one({"_id": f"alert:{alert['key']}:{day}", "at": now})
        except DuplicateKeyError:
            continue
        await notify_leads_alert(alert["key"], alert["title"], alert["body"])
        done["alerts"] += 1

    return done


@distributed_job_lock("support_lifecycle", ttl_seconds=240)
async def support_lifecycle_tick() -> None:
    try:
        done = await run_support_lifecycle()
        if any(done.values()):
            logger.info("Support lifecycle: %s", done)
    except Exception:
        logger.exception("Support lifecycle tick failed")
