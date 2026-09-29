"""Support triage: cheap, deterministic rules that run on every new ticket.

* a paid plan raises the priority one level (inert until paid plans exist)
* three or more failures in the workspace in the last hour raise it one level
* a member who already has another open ticket gets the tag "repeat"
* a suggested category, from keywords (an AI fallback is Phase 6)
* three or more tickets in two hours about the same category and platform are
  grouped into one incident, so staff fix it once and answer everyone at once
* optionally, the ticket goes straight to whoever owns that area

No LLM is used here. Everything is recorded in the ticket's audit trail.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from app.db.mongo import (
    activity_entries,
    support_incidents,
    support_settings,
    support_tickets,
    users,
)
from app.shared import support_rules as rules
from app.shared import support_sla as sla
from app.shared import support_ai
from app.shared.support import log_event

logger = logging.getLogger(__name__)

_SYSTEM = {"actor_type": "system", "actor_id": None, "actor_name": "Recast"}
_LEVELS = ["P3", "P2", "P1"]  # lowest to highest
_OPEN = ["open", "investigating", "waiting_on_member", "waiting_on_engineering"]

CLUSTER_MIN_TICKETS = 3
CLUSTER_WINDOW = timedelta(hours=2)
ERROR_BURST_MIN = 3
ERROR_BURST_WINDOW = timedelta(hours=1)

# First match wins, most specific first.
_CATEGORY_RULES: list[tuple[str, re.Pattern]] = [
    ("Billing", re.compile(r"\b(bill|billing|invoice|payment|refund|charge|subscription|price|plan)\w*", re.I)),
    ("Connecting accounts", re.compile(r"\b(connect|reconnect|oauth|token|authori[sz]e|disconnect|permission)\w*", re.I)),
    ("Publishing", re.compile(r"\b(publish|schedul|post(ed|ing)?|stuck|queue|linkedin|instagram|facebook|threads|bluesky|youtube)\w*", re.I)),
    ("Brand voice", re.compile(r"\b(brand|voice|tone|style|persona)\w*", re.I)),
    ("Something's broken", re.compile(r"\b(error|broken|crash|bug|fail|failed|not working|doesn'?t work)\w*", re.I)),
]


def suggest_category(subject: str, body: str) -> Optional[str]:
    text = f"{subject}\n{body}"
    for name, pattern in _CATEGORY_RULES:
        if pattern.search(text):
            return name
    return None


def _raise_level(priority: str) -> str:
    idx = _LEVELS.index(priority) if priority in _LEVELS else 1
    return _LEVELS[min(idx + 1, len(_LEVELS) - 1)]


async def get_routing() -> dict[str, str]:
    """area (category) -> staff user id, edited by an admin."""
    doc = await support_settings.find_one({"_id": "routing"})
    return dict((doc or {}).get("owners") or {})


def _platform_of(ticket: dict) -> Optional[str]:
    src = ticket.get("source_context") or {}
    return src.get("id") if src.get("type") == "platform" else None


async def _cluster(ticket: dict, now: datetime) -> Optional[dict]:
    """Attach to a matching open incident, or open one once enough tickets pile up."""
    category, platform = ticket.get("category"), _platform_of(ticket)
    if not category:
        return None
    existing = await support_incidents.find_one(
        {"category": category, "platform": platform, "status": {"$in": ["open", "monitoring"]}}
    )
    if existing:
        await support_incidents.update_one(
            {"id": existing["id"]}, {"$addToSet": {"ticket_ids": ticket["id"]}, "$set": {"updated_at": now}}
        )
        return existing

    query: dict = {
        "category": category,
        "status": {"$in": _OPEN},
        "created_at": {"$gte": now - CLUSTER_WINDOW},
        "incident_id": None,
    }
    if platform:
        query["source_context.type"] = "platform"
        query["source_context.id"] = platform
    else:
        query["$or"] = [{"source_context": None}, {"source_context.type": {"$ne": "platform"}}]
    similar = await support_tickets.find(query).to_list(50)
    if not any(t["id"] == ticket["id"] for t in similar):
        similar.append(ticket)
    if len(similar) < CLUSTER_MIN_TICKETS:
        return None

    incident = {
        "id": str(uuid4()),
        "title": f"{category}{f' on {platform}' if platform else ''}: several members are affected",
        "category": category,
        "platform": platform,
        "status": "open",
        "ticket_ids": [t["id"] for t in similar],
        "eng_issue_url": None,
        "created_by": "system",
        "created_by_name": "Recast",
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
    }
    await support_incidents.insert_one(incident)
    await support_tickets.update_many({"id": {"$in": incident["ticket_ids"]}}, {"$set": {"incident_id": incident["id"]}})
    for tid in incident["ticket_ids"]:
        await log_event(tid, type="incident_linked", data={"incident_id": incident["id"]}, **_SYSTEM)
    return incident


async def triage_ticket(ticket_id: str) -> dict:
    """Run every rule once. Never raises; returns what it did."""
    did: dict = {}
    try:
        ticket = await support_tickets.find_one({"id": ticket_id})
        if not ticket:
            return did
        now = datetime.now(timezone.utc)
        sets: dict = {}

        # 1 and 2: priority
        priority = ticket["severity"]
        reasons: list[str] = []
        if ticket.get("workspace_tier") in rules.PAID_TIERS:
            priority = _raise_level(priority)
            reasons.append("paid plan")
        burst = await activity_entries.count_documents(
            {
                "workspace_id": ticket["workspace_id"],
                "status": "failed",
                "occurred_at": {"$gte": now - ERROR_BURST_WINDOW},
            }
        )
        if burst >= ERROR_BURST_MIN:
            priority = _raise_level(priority)
            reasons.append(f"{burst} failures in the last hour")
        if priority != ticket["severity"]:
            sets["severity"] = priority
            sets["sla"] = sla.compute(ticket["created_at"], priority, ticket.get("workspace_tier"), await sla.get_config(), ticket.get("sla"))
            await log_event(ticket_id, type="priority_changed", data={"from": ticket["severity"], "to": priority, "why": ", ".join(reasons)}, **_SYSTEM)
            did["priority"] = priority

        # 3: repeat
        other = await support_tickets.count_documents(
            {"created_by": ticket["created_by"], "status": {"$in": _OPEN}, "id": {"$ne": ticket_id}}
        )
        if other:
            tags = list(ticket.get("tags", []))
            if "repeat" not in tags:
                sets["tags"] = [*tags, "repeat"]
                await log_event(ticket_id, type="tags_changed", data={"tags": sets["tags"], "why": "another open ticket"}, **_SYSTEM)
                did["repeat"] = True

        # 4: suggested category
        first_text = next((m["text"] for m in ticket.get("messages", []) if not m.get("is_internal")), "")
        suggestion = suggest_category(ticket.get("subject", ""), first_text)
        if suggestion is None and rules.AI_CATEGORY_FALLBACK_ENABLED:
            suggestion = await support_ai.ai_category(ticket)
        if suggestion and suggestion != ticket.get("category"):
            sets["suggested_category"] = suggestion
            did["suggested_category"] = suggestion

        if sets:
            await support_tickets.update_one({"id": ticket_id}, {"$set": sets})
            ticket.update(sets)

        # 5: incident
        incident = await _cluster(ticket, now)
        if incident:
            await support_tickets.update_one({"id": ticket_id}, {"$set": {"incident_id": incident["id"]}})
            did["incident_id"] = incident["id"]
            if incident["created_at"] != now and ticket_id not in incident.get("ticket_ids", []):
                await log_event(ticket_id, type="incident_linked", data={"incident_id": incident["id"]}, **_SYSTEM)

        # 6: route to the area owner
        if not ticket.get("assignee_id"):
            owner_id = (await get_routing()).get(ticket.get("category", ""))
            if owner_id:
                owner = await users.find_one({"id": owner_id})
                if owner and (owner.get("is_platform_staff") or owner.get("is_master_admin")):
                    await support_tickets.update_one(
                        {"id": ticket_id, "assignee_id": None},
                        {"$set": {"assignee_id": owner_id, "assignee_name": owner.get("name", "")}},
                    )
                    await log_event(ticket_id, type="assigned", data={"to": owner_id, "to_name": owner.get("name", ""), "why": "area owner"}, **_SYSTEM)
                    did["assigned_to"] = owner_id
    except Exception:
        logger.warning("Support triage failed for ticket %s", ticket_id, exc_info=True)
    return did
