"""Support tickets — the one place that owns the rules.

Both the member routes (``app.api.v1.support``) and the staff routes
(``app.api.v1.ops_support``) go through here so the status rules, the
member-visible view of a ticket and the audit trail cannot drift apart.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import Depends, HTTPException

from app.core.auth import require_platform_staff
from app.db.mongo import support_counters, support_ticket_events
from app.models.support import (
    SupportStaffRole,
    SupportTicketMemberView,
    SupportTicketStatus as S,
)

logger = logging.getLogger(__name__)

# ── Status rules ─────────────────────────────────────────────────────────────
# The single transition table. Every status change, from either side and from
# every route, is checked against it.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    S.OPEN.value: frozenset({S.INVESTIGATING.value, S.RESOLVED.value, S.CLOSED.value}),
    S.INVESTIGATING.value: frozenset(
        {S.WAITING_ON_MEMBER.value, S.WAITING_ON_ENGINEERING.value, S.RESOLVED.value, S.CLOSED.value}
    ),
    S.WAITING_ON_MEMBER.value: frozenset({S.INVESTIGATING.value, S.RESOLVED.value, S.CLOSED.value}),
    S.WAITING_ON_ENGINEERING.value: frozenset({S.INVESTIGATING.value, S.RESOLVED.value, S.CLOSED.value}),
    S.RESOLVED.value: frozenset({S.INVESTIGATING.value, S.CLOSED.value}),
    S.CLOSED.value: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def transition_fields(current: str, target: str, now: datetime) -> dict[str, Any]:
    """The ``$set`` fields for a status change, or a 409 if it isn't allowed."""
    if not can_transition(current, target):
        raise HTTPException(
            status_code=409, detail=f"A ticket can't go from {current} to {target}."
        )
    fields: dict[str, Any] = {"status": target}
    if target == S.RESOLVED.value:
        fields["resolved_at"] = now
    elif target == S.CLOSED.value:
        fields["closed_at"] = now
    elif current == S.RESOLVED.value:
        # Reopened: it is no longer resolved.
        fields["resolved_at"] = None
    return fields


# ── Member-visible view ──────────────────────────────────────────────────────
def member_view(
    ticket: dict,
    *,
    viewer_id: Optional[str] = None,
    limit: Optional[int] = None,
    before: Optional[int] = None,
) -> SupportTicketMemberView:
    """The only way a stored ticket is turned into something a member sees.

    Drops internal notes and every staff-only field (assignee id, tags,
    workspace and member ids of other people). ``limit`` and ``before`` page a
    long thread: the newest ``limit`` visible messages, or the ``limit``
    before index ``before`` when loading older ones.
    """
    visible = [m for m in ticket.get("messages", []) if not m.get("is_internal", False)]
    total = len(visible)
    if limit is None:
        start, end = 0, total
    else:
        end = total if before is None else max(0, min(before, total))
        start = max(0, end - limit)
    return SupportTicketMemberView(
        **{
            **ticket,
            "messages": visible[start:end],
            "message_total": total,
            "has_older": start > 0,
            "first_index": start,
            "is_own": viewer_id is None or ticket.get("created_by") == viewer_id,
        }
    )


# ── Audit trail ──────────────────────────────────────────────────────────────
async def log_event(
    ticket_id: str,
    *,
    actor_type: str,
    actor_id: Optional[str],
    actor_name: Optional[str],
    type: str,
    data: Optional[dict] = None,
) -> None:
    """Append one row. Never raises: a failed audit write must not fail the action."""
    try:
        await support_ticket_events.insert_one(
            {
                "id": str(uuid4()),
                "ticket_id": ticket_id,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "actor_name": actor_name,
                "type": type,
                "data": data or {},
                "created_at": datetime.now(timezone.utc),
            }
        )
    except Exception:
        logger.warning("Support audit write failed for ticket %s (%s)", ticket_id, type, exc_info=True)


async def next_ticket_number() -> int:
    doc = await support_counters.find_one_and_update(
        {"_id": "ticket_number"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return int(doc["seq"])


# ── Staff roles ──────────────────────────────────────────────────────────────
_ROLE_RANK = {SupportStaffRole.AGENT.value: 1, SupportStaffRole.LEAD.value: 2, SupportStaffRole.ADMIN.value: 3}


def staff_role(user: dict) -> str:
    """A master admin is a support admin; otherwise the stored role, default agent."""
    if user.get("is_master_admin"):
        return SupportStaffRole.ADMIN.value
    role = user.get("support_role")
    return role if role in _ROLE_RANK else SupportStaffRole.AGENT.value


def has_role_at_least(user: dict, minimum: str) -> bool:
    return _ROLE_RANK[staff_role(user)] >= _ROLE_RANK[minimum]


def require_support_lead(user: dict = Depends(require_platform_staff)) -> dict:
    if not has_role_at_least(user, SupportStaffRole.LEAD.value):
        raise HTTPException(status_code=403, detail="Support lead access required.")
    return user
