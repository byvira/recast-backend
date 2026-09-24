"""Cadence monitoring — Odette watches whether automated campaigns keep the
workspace posting at the rhythm it asked for.

The cadence is the one the workspace already configured on its campaigns
(``cadence.frequency`` daily/weekly, ``days_per_batch``, ``platforms``) —
nothing new to set up. Two checks, hourly:

1. **Stalled** — an automated campaign more than one full cadence period
   past its ``next_run_at`` (it keeps failing, see the scheduler's backoff).
   → Odette flag ``campaign_stalled`` in the admins' Active lane; resolved
   automatically once the campaign generates again.
2. **Behind** — the campaign is generating, but fewer than half of the posts
   its cadence implies went out in the last 7 days while its drafts sit
   unpublished. → one Odette recommendation (not repeated within 7 days)
   saying how many drafts are waiting.

Drafts are never published from here — this only tells people.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import content_pieces, get_campaigns_collection, workspace_flags, workspace_insights
from app.shared.activity import project_odette_flag, project_odette_insight

logger = logging.getLogger(__name__)

_PERIOD = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1)}
STALLED_FLAG = "campaign_stalled"
BEHIND_RATIO = 0.5
WINDOW = timedelta(days=7)
REPEAT_WINDOW = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expected_posts_per_week(campaign: dict) -> int:
    """Posts the configured cadence generates per week: one batch per
    period, each batch ``days_per_batch`` days × the campaign's platforms (or
    its per-day platform lists when set). Weekly → one batch a week; daily →
    seven."""
    cadence = campaign.get("cadence") or {}
    frequency = cadence.get("frequency", "manual")
    if frequency not in _PERIOD:
        return 0
    days = int(cadence.get("days_per_batch") or 7)
    by_day = campaign.get("platforms_by_day")
    per_batch = sum(len(d) for d in by_day[:days]) if by_day else days * len(campaign.get("platforms") or [])
    return per_batch * (7 if frequency == "daily" else 1)


async def _open_stall_flag(campaign: dict) -> Optional[dict]:
    return await workspace_flags.find_one({
        "workspace_id": campaign["workspace_id"], "flag_type": STALLED_FLAG,
        "status": "open", "detail.campaign_id": campaign["id"],
    })


async def check_stalled(campaign: dict, now: datetime) -> Optional[str]:
    period = _PERIOD.get((campaign.get("cadence") or {}).get("frequency"))
    next_run = _aware((campaign.get("cadence") or {}).get("next_run_at"))
    existing = await _open_stall_flag(campaign)
    last_generated = _aware(campaign.get("last_generated_at"))

    if existing:
        created = _aware(existing.get("created_at"))
        if last_generated and created and last_generated > created:
            await workspace_flags.update_one(
                {"_id": existing["_id"]},
                {"$set": {"status": "resolved", "resolved_at": now, "resolved_by": "system"}},
            )
            existing.update(status="resolved", resolved_at=now, resolved_by="system")
            await project_odette_flag(existing)
        return None

    if not period or not next_run or now - next_run <= period:
        return None
    failures = int((campaign.get("cadence") or {}).get("failures") or 0)
    error = (campaign.get("cadence") or {}).get("last_error") or "it hasn't been able to run"
    flag = {
        "_id": str(uuid4()),
        "workspace_id": campaign["workspace_id"],
        "flag_type": STALLED_FLAG,
        "detection": "rule",
        "severity": "warning",
        "summary_persona": (
            f"\"{campaign.get('name') or 'A campaign'}\" is behind its "
            f"{campaign['cadence']['frequency']} schedule — {failures or 'several'} failed "
            f"attempts. Last error: {error} Fix the cause and it resumes on its own."
        ),
        "detail": {"campaign_id": campaign["id"], "failures": failures, "last_error": error},
        "metric": {"name": "overdue_hours", "value": round((now - next_run).total_seconds() / 3600, 1),
                   "limit": round(period.total_seconds() / 3600, 1)},
        "langsmith_run_url": None,
        "status": "open",
        "notified": {"in_app": True, "email": False, "at": now},
        "created_at": now,
        "resolved_at": None,
    }
    await workspace_flags.insert_one(flag)
    await project_odette_flag(flag)
    return flag["_id"]


async def check_behind(campaign: dict, now: datetime) -> Optional[str]:
    expected = expected_posts_per_week(campaign)
    if not expected or not campaign.get("piece_ids"):
        return None
    if await workspace_insights.find_one({
        "workspace_id": campaign["workspace_id"], "evidence.metrics.source": "cadence",
        "evidence.metrics.campaign_id": campaign["id"], "created_at": {"$gte": now - REPEAT_WINDOW},
    }):
        return None

    base = {"campaign_id": campaign["id"], "workspace_id": campaign["workspace_id"], "deleted": {"$ne": True}}
    published = await content_pieces.count_documents({
        **base, "publish_status": "published",
        "$or": [{"published_at": {"$gte": now - WINDOW}},
                {"published_at": {"$exists": False}, "updated_at": {"$gte": now - WINDOW}}],
    })
    waiting = await content_pieces.count_documents({
        **base, "publish_status": {"$in": ["pending", None]}, "approval_status": {"$ne": "rejected"},
    })
    if published >= expected * BEHIND_RATIO or waiting == 0:
        return None

    name = campaign.get("name") or "A campaign"
    doc = {
        "_id": str(uuid4()),
        "workspace_id": campaign["workspace_id"],
        "kind": "recommendation",
        "title": f"\"{name}\" is behind its posting cadence",
        "body_persona": (
            f"Its {campaign['cadence']['frequency']} cadence implies about {expected} posts a week; "
            f"{published} went out in the last 7 days. {waiting} of its drafts are waiting for review — "
            f"approving or scheduling them is the quickest way back on pace."
        ),
        "rationale": f"published_7d={published}, expected_per_week={expected}, drafts_waiting={waiting}",
        "evidence": {"event_ids": [], "signal_ids": [], "metrics": {
            "source": "cadence", "campaign_id": campaign["id"],
            "published_7d": published, "expected_per_week": expected, "waiting": waiting,
        }},
        "priority": "medium",
        "pipeline_scope": "all",
        "langsmith_run_url": "",
        "status": "new",
        "created_at": now,
        "updated_at": now,
        "created_by_agent_run": f"cadence:{campaign['id']}:{now.isoformat()}",
    }
    await workspace_insights.insert_one(doc)
    await project_odette_insight(doc)
    return doc["_id"]


@distributed_job_lock("cadence_monitor", ttl_seconds=900)
async def cadence_monitor(ctx: Optional[dict] = None) -> dict:
    now = _now()
    stalled = behind = 0
    async for campaign in get_campaigns_collection().find({
        "deleted": {"$ne": True},
        "status": {"$nin": ["paused", "completed"]},
        "cadence.frequency": {"$in": list(_PERIOD)},
    }):
        try:
            if await check_stalled(campaign, now):
                stalled += 1
            if await check_behind(campaign, now):
                behind += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("cadence check failed for campaign %s: %s", campaign.get("id"), exc)
    return {"stalled": stalled, "behind": behind}
