"""Support numbers for the metrics page, and the alerts raised from them.

Everything is read from data the ticket system already stores. Where there is
too little data for a number to mean anything, the value is null instead of a
misleading zero.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db.mongo import (
    support_ai_usage,
    support_email_log,
    support_ticket_context,
    support_ticket_events,
    support_tickets,
    workspace_ai_usage_daily,
)

ENRICH_ALERT_RATE = 0.10
ENRICH_ALERT_MIN_SAMPLES = 5
EMAIL_ALERT_RATE = 0.20
EMAIL_ALERT_MIN_FAILED = 3


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _seconds(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    a, b = _utc(a), _utc(b)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def _summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "median": None, "p90": None}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {"count": len(values), "median": statistics.median(ordered), "p90": p90}


def _rate(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 4) if whole else None


async def compute(start: datetime, end: datetime) -> dict:
    created = await support_tickets.find({"created_at": {"$gte": start, "$lte": end}}).to_list(5000)
    resolved = await support_tickets.find({"resolved_at": {"$gte": start, "$lte": end}}).to_list(5000)

    by_category = Counter(t.get("category", "Other") for t in created)
    by_platform: Counter = Counter()
    by_day: Counter = Counter()
    for t in created:
        src = t.get("source_context") or {}
        by_platform[src.get("id") if src.get("type") == "platform" else "none"] += 1
        by_day[_utc(t["created_at"]).strftime("%Y-%m-%d")] += 1

    first_reply = [
        s for t in created
        if (s := _seconds(t["created_at"], (t.get("sla") or {}).get("first_responded_at"))) is not None
    ]
    resolution = [
        s for t in resolved if (s := _seconds(t["created_at"], t.get("resolved_at"))) is not None
    ]

    agents: dict[str, dict] = defaultdict(lambda: {"assigned": 0, "resolved": 0})
    for t in created:
        if t.get("assignee_name"):
            agents[t["assignee_name"]]["assigned"] += 1
    for t in resolved:
        if t.get("assignee_name"):
            agents[t["assignee_name"]]["resolved"] += 1

    reopened_ids = set(
        await support_ticket_events.distinct("ticket_id", {"type": "reopened", "created_at": {"$gte": start, "$lte": end}})
    )

    rated = await support_tickets.find({"rating.at": {"$gte": start, "$lte": end}}, {"rating": 1}).to_list(5000)
    up = sum(1 for t in rated if t["rating"]["value"] == "up")
    down = sum(1 for t in rated if t["rating"]["value"] == "down")

    with_sla = [t for t in created if t.get("sla")]
    breached = sum(1 for t in with_sla if t["sla"].get("breached_first") or t["sla"].get("breached_resolve"))

    ctx_total = await support_ticket_context.count_documents({"created_at": {"$gte": start, "$lte": end}})
    ctx_failed = await support_ticket_context.count_documents(
        {"created_at": {"$gte": start, "$lte": end}, "enrich_status": "failed"}
    )
    email_total = await support_email_log.count_documents({"at": {"$gte": start, "$lte": end}})
    email_failed = await support_email_log.count_documents({"at": {"$gte": start, "$lte": end}, "ok": False})

    ai_rows = await support_ai_usage.find({"created_at": {"$gte": start, "$lte": end}}).to_list(5000)
    day_from, day_to = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    usage_rows = await workspace_ai_usage_daily.find(
        {"workspace_id": "support-platform", "date": {"$gte": day_from, "$lte": day_to}}
    ).to_list(400)

    return {
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "volume": {
            "total": len(created),
            "by_category": dict(by_category),
            "by_platform": dict(by_platform),
            "by_day": dict(sorted(by_day.items())),
        },
        "first_response_seconds": _summary(first_reply),
        "resolution_seconds": _summary(resolution),
        "per_agent": sorted(
            ({"name": n, **v} for n, v in agents.items()), key=lambda a: (-a["resolved"], -a["assigned"])
        ),
        "reopen_rate": _rate(len(reopened_ids), len(resolved)),
        "rating": {"up": up, "down": down, "up_share": _rate(up, up + down)},
        "sla_breach_rate": _rate(breached, len(with_sla)),
        "enrich_failure_rate": _rate(ctx_failed, ctx_total),
        "email": {"attempts": email_total, "failed": email_failed, "failure_rate": _rate(email_failed, email_total)},
        "ai": {
            "calls": len(ai_rows),
            "failed": sum(1 for r in ai_rows if not r.get("ok")),
            "drafts": sum(1 for r in ai_rows if r.get("kind") == "draft"),
            "tokens": sum(int(r.get("tokens_used", 0) or 0) for r in usage_rows),
            "by_prompt": dict(Counter(r.get("prompt_version", "?") for r in ai_rows)),
        },
    }


async def active_alerts(now: Optional[datetime] = None) -> list[dict]:
    """Things that need a person's attention right now."""
    now = now or datetime.now(timezone.utc)
    alerts: list[dict] = []

    since = now - timedelta(days=1)
    ctx_total = await support_ticket_context.count_documents({"created_at": {"$gte": since}})
    ctx_failed = await support_ticket_context.count_documents({"created_at": {"$gte": since}, "enrich_status": "failed"})
    if ctx_total >= ENRICH_ALERT_MIN_SAMPLES and ctx_failed / ctx_total > ENRICH_ALERT_RATE:
        alerts.append(
            {"key": "enrich_failures", "title": "Ticket context is failing to build",
             "body": f"{ctx_failed} of {ctx_total} tickets in the last day have no context snapshot."}
        )

    email_total = await support_email_log.count_documents({"at": {"$gte": since}})
    email_failed = await support_email_log.count_documents({"at": {"$gte": since}, "ok": False})
    if email_failed >= EMAIL_ALERT_MIN_FAILED and email_failed / max(1, email_total) > EMAIL_ALERT_RATE:
        alerts.append(
            {"key": "email_failures", "title": "Support emails are failing",
             "body": f"{email_failed} of {email_total} emails in the last day did not send. Members still see updates in the app."}
        )

    late = await support_tickets.count_documents(
        {
            "status": {"$nin": ["resolved", "closed"]},
            "$or": [{"assignee_id": None}, {"assignee_id": {"$exists": False}}],
            "sla.first_responded_at": None,
            "sla.first_response_due": {"$lt": now},
        }
    )
    if late:
        alerts.append(
            {"key": "unassigned_late", "title": f"{late} unassigned ticket{'s are' if late != 1 else ' is'} past the first reply time",
             "body": "Nobody has taken these and their time for a first reply has passed."}
        )
    return alerts
