"""Support SLA: how long a ticket may wait for a first reply and for a fix.

Targets come from ``support_rules`` and can be overridden by an admin (stored
in ``support_settings``). Paid plans get one priority level faster; there are
no paid plans yet, see ``support_rules.PAID_TIERS``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db.mongo import support_settings
from app.shared import support_rules as rules

_FASTER = {"P1": "P1", "P2": "P1", "P3": "P2"}


def default_config() -> dict:
    return {
        "first_response_hours": dict(rules.SLA_FIRST_RESPONSE_HOURS),
        "resolution_hours": dict(rules.SLA_RESOLUTION_HOURS),
    }


async def get_config() -> dict:
    """The defaults with any admin overrides applied."""
    config = default_config()
    doc = await support_settings.find_one({"_id": "sla"})
    if doc:
        for key in ("first_response_hours", "resolution_hours"):
            for prio, hours in (doc.get(key) or {}).items():
                if prio in config[key] and isinstance(hours, (int, float)) and hours > 0:
                    config[key][prio] = hours
    return config


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def compute(created_at: datetime, priority: str, tier: Optional[str], config: dict, previous: Optional[dict] = None) -> dict:
    """The SLA block stored on a ticket. ``previous`` keeps what already happened
    (when it was first answered, which breaches were already flagged) when the
    targets are recalculated after a priority change."""
    effective = _FASTER.get(priority, priority) if tier in rules.PAID_TIERS else priority
    created = _as_utc(created_at)
    previous = previous or {}
    return {
        "first_response_due": created + timedelta(hours=config["first_response_hours"][effective]),
        "resolve_due": created + timedelta(hours=config["resolution_hours"][effective]),
        "first_responded_at": previous.get("first_responded_at"),
        "breached_first": bool(previous.get("breached_first", False)),
        "breached_resolve": bool(previous.get("breached_resolve", False)),
        "digest_sent": bool(previous.get("digest_sent", False)),
    }
