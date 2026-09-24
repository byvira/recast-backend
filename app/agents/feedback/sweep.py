"""The feedback loop: real engagement → Remy's coaching and Odette's workspace
recommendations, tempered by what people did with earlier suggestions.

Daily sweep (``app.workers.jobs``):

* **Remy** — per member with enough measured posts: the strongest pattern
  becomes one ``performance_pattern`` signal in that member's Active lane.
* **Odette** — per workspace: the strongest workspace-wide pattern becomes one
  insight in the admins' Active lane.

Feedback rules (the "learning" part):

* A pattern variant someone has **dismissed twice** in the last 60 days is not
  suggested to them again — their decisions outweigh the arithmetic.
* The same finding is never repeated within 30 days, whatever its outcome.
* At most one new coaching item per member (or workspace) per 7 days, so the
  Active lane doesn't turn into a report feed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from app.agents.feedback.patterns import Finding, find_patterns, load_samples
from app.agents.personal.signals import emit_signal
from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import (
    personal_signals,
    post_metric_checkpoints,
    users,
    workspace_insights,
    workspaces,
)
from app.prompts.registry import load_localized
from app.shared.activity import project_odette_insight
from app.shared.language import first_present, user_language, workspace_language
from app.shared.localized_strings import get_localized_string

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "performance_pattern"
MIN_SAMPLES = 10
REPEAT_WINDOW = timedelta(days=30)
DISMISS_WINDOW = timedelta(days=60)
DISMISS_LIMIT = 2
CADENCE = timedelta(days=7)

_TEMPLATES = load_localized("performance_patterns")

_ODETTE_TITLES = {
    "platform": "{winner} is where your content lands best",
    "length_short": "Shorter posts are outperforming",
    "length_long": "Longer posts are outperforming",
    "opener_question": "Question openers are outperforming",
    "opener_statement": "Statement openers are outperforming",
    "time": "Posts in the {start}:00–{end}:00 window are outperforming",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _render(voice: str, finding: Finding, language: str) -> str:
    key = f"{voice}.{finding.variant}"
    ctx = {"ratio": finding.ratio, "n": finding.n, **finding.params}
    return await get_localized_string(f"performance.{key}", language, _TEMPLATES[key], ctx)


def _choose(findings: list[Finding], *, recent_keys: set[str], dismissed: dict[str, int]) -> Optional[Finding]:
    for f in findings:
        if f.key in recent_keys:
            continue
        if dismissed.get(f.variant, 0) >= DISMISS_LIMIT:
            continue
        return f
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Remy — one member
# ─────────────────────────────────────────────────────────────────────────────

async def coach_member(workspace_id: str, user_id: str) -> Optional[str]:
    now = _now()
    history = await personal_signals.find(
        {"workspace_id": workspace_id, "user_id": user_id, "signal_type": SIGNAL_TYPE,
         "created_at": {"$gte": now - DISMISS_WINDOW}},
        {"metric.name": 1, "status": 1, "created_at": 1, "pattern_key": 1},
    ).to_list(length=200)
    if any(_aware(h["created_at"]) >= now - CADENCE for h in history):
        return None

    samples = await load_samples(workspace_id, user_id)
    if len(samples) < MIN_SAMPLES:
        return None
    user = await users.find_one({"id": user_id}, {"timezone": 1}) or {}
    finding = _choose(
        find_patterns(samples, user.get("timezone") or "UTC"),
        recent_keys={h.get("pattern_key") for h in history if _aware(h["created_at"]) >= now - REPEAT_WINDOW},
        dismissed=_count_dismissed(history, status="dismissed"),
    )
    if not finding:
        return None

    language = first_present(await user_language(user_id), await workspace_language(workspace_id))
    signal_id = await emit_signal(
        workspace_id=workspace_id, user_id=user_id, pipeline_type=None,
        signal_type=SIGNAL_TYPE, severity="low",
        metric={"name": finding.variant, "value": finding.ratio, "baseline": 1.0, "threshold": 1.5},
        window={"kind": "rolling", "n": finding.n},
        evidence_refs=[],
        member_message=await _render("remy", finding, language),
        supervisor_note=f"engagement pattern {finding.key}: {finding.ratio}x over {finding.n} posts at 24h",
    )
    await personal_signals.update_one({"_id": signal_id}, {"$set": {"pattern_key": finding.key}})
    return signal_id


def _count_dismissed(history: list[dict], *, status: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for h in history:
        if h.get("status") == status:
            variant = (h.get("metric") or {}).get("name") or ""
            counts[variant] = counts.get(variant, 0) + 1
    return counts


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Odette — whole workspace
# ─────────────────────────────────────────────────────────────────────────────

async def advise_workspace(workspace_id: str) -> Optional[str]:
    now = _now()
    history = await workspace_insights.find(
        {"workspace_id": workspace_id, "evidence.metrics.source": SIGNAL_TYPE,
         "created_at": {"$gte": now - DISMISS_WINDOW}},
        {"status": 1, "created_at": 1, "evidence.metrics": 1},
    ).to_list(length=200)
    if any(_aware(h["created_at"]) >= now - CADENCE for h in history):
        return None

    samples = await load_samples(workspace_id)
    if len(samples) < MIN_SAMPLES:
        return None
    dismissed: dict[str, int] = {}
    recent: set[str] = set()
    for h in history:
        metrics = (h.get("evidence") or {}).get("metrics") or {}
        if h.get("status") == "dismissed":
            dismissed[metrics.get("variant", "")] = dismissed.get(metrics.get("variant", ""), 0) + 1
        if _aware(h["created_at"]) >= now - REPEAT_WINDOW:
            recent.add(metrics.get("pattern_key", ""))
    finding = _choose(find_patterns(samples, "UTC"), recent_keys=recent, dismissed=dismissed)
    if not finding:
        return None

    ws = await workspaces.find_one({"id": workspace_id}, {"language": 1}) or {}
    language = ws.get("language") or "en"
    insight_id = str(uuid4())
    doc = {
        "_id": insight_id,
        "workspace_id": workspace_id,
        "kind": "recommendation",
        "title": _ODETTE_TITLES[finding.variant].format(**finding.params),
        "body_persona": await _render("odette", finding, language),
        "rationale": f"Median engagement rate {finding.ratio}× the rest, {finding.n} posts measured 24h after publishing.",
        "evidence": {"event_ids": [], "signal_ids": [], "metrics": {
            "source": SIGNAL_TYPE, "variant": finding.variant, "pattern_key": finding.key,
            "ratio": finding.ratio, "posts": finding.n,
        }},
        "priority": "medium",
        "pipeline_scope": "all",
        "langsmith_run_url": "",
        "status": "new",
        "created_at": now,
        "updated_at": now,
        "created_by_agent_run": f"feedback:{workspace_id}:{now.isoformat()}",
    }
    await workspace_insights.insert_one(doc)
    await project_odette_insight(doc)
    return insight_id


# ─────────────────────────────────────────────────────────────────────────────
# Job
# ─────────────────────────────────────────────────────────────────────────────

@distributed_job_lock("performance_feedback_sweep", ttl_seconds=1800)
async def performance_feedback_sweep(ctx: Optional[dict] = None) -> dict:
    """Daily: coach every member and advise every workspace that has enough
    measured posts. Workspaces with no 24h checkpoints are never touched."""
    since = _now() - timedelta(days=90)
    remy = odette = 0
    for workspace_id in await post_metric_checkpoints.distinct(
        "workspace_id", {"checkpoint": "24h", "captured_at": {"$gte": since}}
    ):
        try:
            for user_id in await post_metric_checkpoints.distinct(
                "user_id", {"workspace_id": workspace_id, "checkpoint": "24h", "captured_at": {"$gte": since}}
            ):
                if user_id and await coach_member(workspace_id, user_id):
                    remy += 1
            if await advise_workspace(workspace_id):
                odette += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("feedback sweep failed for workspace %s: %s", workspace_id, exc)
    if remy or odette:
        logger.info("feedback sweep: remy=%d odette=%d", remy, odette)
    return {"remy": remy, "odette": odette}
