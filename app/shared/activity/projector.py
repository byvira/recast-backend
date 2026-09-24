"""Turn the things Recast already records into Activity Log rows.

Four sources, one row shape:

* ``workspace_events``  → Passive rows (``project_event``) — people and
  pipelines doing work.
* ``personal_signals``  → Remy's feedback (``project_remy_signal``) — Active
  until the member decides, member-private.
* ``workspace_insights`` / ``workspace_flags`` → Odette's recommendations and
  alerts (``project_odette_insight`` / ``project_odette_flag``) — Active until
  decided, admin-only (the same gate as ``/api/v1/supervisor``).
* system jobs → Passive rows (``record_system``) — autonomous work such as
  scheduled publishing, retries and token refreshes. These go straight to the
  Activity Log and **not** onto the event bus, so Odette's LLM passes don't pay
  for routine housekeeping.

Every function here is safe to call from a hot path: it never raises.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.shared.activity import actors
from app.shared.activity.store import LANE_ACTIVE, LANE_PASSIVE, upsert_entry

logger = logging.getLogger(__name__)

CATEGORY_RECOMMENDATION = "recommendation"
CATEGORY_WORKSPACE_ALERT = "workspace_alert"

# ─────────────────────────────────────────────────────────────────────────────
# Titles — English, like every other string in the dashboard's copy constants.
# The descriptions carry the agent's own (localized) voice.
# ─────────────────────────────────────────────────────────────────────────────

_REMY_TITLES = {
    "voice_drift": "This piece drifts from your usual voice",
    "voice_drift_trend": "Your recent pieces are drifting from your voice",
    "volume_spike": "Publishing much more than usual today",
    "volume_drop": "You've gone quiet after a steady stretch",
    "platform_volume_drop": "A channel has gone quiet",
    "topic_shift": "Your topics have shifted",
    "quality_regression": "More drafts than usual flagged for review",
    "performance_pattern": "Something in your posts is working",
}

_FLAG_TITLES = {
    "tier_seat_exceeded": "Seats over plan limit",
    "daily_publish_cap": "Daily publishing cap reached",
    "rbac_violation": "Action outside a member's role",
    "brand_voice_instability": "Brand voice edited repeatedly",
    "member_churn": "Unusual member changes",
    "assistant_signal_storm": "Many members flagged at once",
    "llm_anomaly": "Unusual workspace activity",
    "connection_broken": "A connected channel needs reconnecting",
    "campaign_stalled": "A campaign has stopped generating",
}

_REMY_DECIDED = {"acknowledged": "accepted", "dismissed": "dismissed"}
_INSIGHT_DECIDED = {"actioned": "accepted", "dismissed": "dismissed"}
_FLAG_DECIDED = {"resolved": "accepted", "muted": "dismissed"}


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


_PIECE_TARGETS = {"Draft Post", "Live Post", "Scheduled Post"}


async def piece_label(piece_id: Optional[str]) -> Optional[str]:
    """First line of a piece's content, trimmed — how people recognise a post."""
    if not piece_id:
        return None
    from app.db.mongo import content_pieces
    piece = await content_pieces.find_one({"piece_id": piece_id}, {"content": 1})
    text = ((piece or {}).get("content") or "").strip()
    if not text:
        return None
    first = text.splitlines()[0].strip()
    return first if len(first) <= 70 else first[:67].rstrip() + "…"


def _humanize(slug: str) -> str:
    return (slug or "").replace("_", " ").strip().capitalize()


# ─────────────────────────────────────────────────────────────────────────────
# workspace_events → Passive
# ─────────────────────────────────────────────────────────────────────────────

async def project_event(event: dict) -> None:
    """Project one persisted ``workspace_events`` document. Event types with no
    Activity Log meaning (``content.created`` — covered by the run summary —
    and ``assistant.signal`` — covered by the signal row) are skipped."""
    try:
        entry = await _event_entry(event)
        if entry:
            await upsert_entry(entry)
        if event.get("event_type") == "pipeline.run_completed":
            # Chaining as a suggestion — see app.agents.feedback.next_steps.
            from app.agents.feedback.next_steps import suggest_after_run
            await suggest_after_run(event)
    except Exception as exc:  # noqa: BLE001
        logger.error("activity project_event failed for %s: %s", event.get("_id"), exc)


async def _event_entry(event: dict) -> Optional[dict]:
    et = event.get("event_type")
    ws = event.get("workspace_id")
    uid = event.get("actor_user_id") or ""
    p = event.get("payload") or {}
    base = {
        "_id": f"event:{event.get('_id') or event.get('event_id')}",
        "workspace_id": ws,
        "lane": LANE_PASSIVE,
        "visibility": "workspace",
        "source": {"kind": "event", "id": event.get("_id") or event.get("event_id"), "type": et},
        "occurred_at": _as_dt(event.get("occurred_at")),
        "status": "success",
    }

    async def member() -> dict:
        return await actors.member_actor(ws, uid, event.get("actor_role") or None)

    if et == "pipeline.run_completed":
        pieces = int(p.get("pieces") or 0)
        failed = int(p.get("failed") or 0)
        platforms = p.get("platforms") or []
        by_campaign = p.get("trigger") == "campaign"
        actor = actors.system_actor("Campaign scheduler") if by_campaign else await member()
        topic = (p.get("title") or "").strip()
        if pieces == 0:
            title = "Pipeline run produced no content"
            status = "failed"
        else:
            title = f"Pipeline finished {pieces} {'output' if pieces == 1 else 'outputs'}"
            status = "warning" if failed else "success"
        description = (
            f"Generated {', '.join(platforms)}" if platforms else f"Generated {pieces} pieces"
        ) + (f" for \"{topic}\"." if topic else ".")
        if failed:
            description += f" {failed} {'output' if failed == 1 else 'outputs'} failed."
        metadata = {"branchesCount": pieces}
        if p.get("duration_ms"):
            metadata["durationSeconds"] = round(int(p["duration_ms"]) / 1000)
        return {
            **base,
            "actor": actor,
            "category": "content_generated",
            "title": title,
            "description": description,
            "channel": platforms[0] if len(platforms) == 1 else None,
            "target_id": p.get("session_id"),
            "target_type": "Pipeline Execution",
            "href": "/dashboard/drafts",
            "status": status,
            "metadata": metadata,
            # Control Tower's "Recently Completed" card fields.
            "subject": topic,
            "platforms": platforms,
        }

    if et == "content.published":
        target = p.get("target") or ""
        scheduled = p.get("via") == "scheduled"
        actor = actors.system_actor("Publishing scheduler") if scheduled else await member()
        metadata = {}
        if p.get("external_url"):
            metadata["publishedUrl"] = p["external_url"]
        return {
            **base,
            "actor": actor,
            "category": "post_published",
            "title": f"Published to {target or 'platform'}",
            # The link lives in href / metadata.publishedUrl (rendered as a
            # real link), not pasted into the sentence.
            "description": (
                f"Scheduled post went live on {target or 'the platform'}."
                if scheduled else f"Post went live on {target or 'the platform'}."
            ),
            "channel": target.lower() or None,
            "target_id": p.get("content_id"),
            "target_type": "Live Post",
            "target_label": await piece_label(p.get("content_id")),
            "href": p.get("external_url") or None,
            "metadata": metadata or None,
        }

    if et in ("member.added", "member.removed"):
        subject = await actors.member_actor(ws, p.get("subject_user_id", ""), p.get("role") or None)
        added = et == "member.added"
        return {
            **base,
            "actor": await member(),
            "category": "security_event",
            "title": "Member joined the workspace" if added else "Member removed from the workspace",
            "description": (
                f"{subject['name']} joined as {subject.get('role', p.get('role') or 'member')}."
                if added else f"{subject['name']} no longer has access to this workspace."
            ),
            "target_type": "Workspace Member",
        }

    if et == "role.changed":
        subject = await actors.member_actor(ws, p.get("subject_user_id", ""), p.get("to_role") or None)
        return {
            **base,
            "actor": await member(),
            "category": "security_event",
            "title": "Member role changed",
            "description": f"{subject['name']}: {_humanize(p.get('from_role'))} → {_humanize(p.get('to_role'))}.",
            "target_type": "Workspace Member",
            "diff": {
                "field": "Role",
                "before": _humanize(p.get("from_role")),
                "after": _humanize(p.get("to_role")),
            },
        }

    if et == "brand.voice_updated":
        fields = p.get("changed_fields") or []
        return {
            **base,
            "actor": await member(),
            "category": "voice_calibrated",
            "title": "Brand voice updated",
            "description": p.get("diff_summary") or (
                f"Changed {', '.join(_humanize(f).lower() for f in fields)}." if fields else "Brand voice settings changed."
            ),
            "target_id": p.get("brand_id"),
            "target_type": "Voice Model",
        }

    if et == "tier.changed":
        return {
            **base,
            "visibility": "admins",
            "actor": await member(),
            "category": "security_event",
            "title": "Workspace plan changed",
            "description": f"{_humanize(p.get('from_tier'))} → {_humanize(p.get('to_tier'))}.",
            "diff": {
                "field": "Plan",
                "before": _humanize(p.get("from_tier")),
                "after": _humanize(p.get("to_tier")),
            },
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Remy (personal) → Active until decided, member-private
# ─────────────────────────────────────────────────────────────────────────────

def remy_entry_id(signal_id: str) -> str:
    return f"remy_signal:{signal_id}"


async def project_remy_signal(signal: dict) -> None:
    try:
        status = signal.get("status", "open")
        outcome = _REMY_DECIDED.get(status)
        metric = signal.get("metric") or {}
        metadata: dict[str, Any] = {"severity": _humanize(signal.get("severity"))}
        if metric.get("name"):
            metadata["signalMetric"] = f"{_humanize(metric['name'])}: {metric.get('value')}"
        if outcome:
            metadata["decision"] = outcome.capitalize()
        evidence = signal.get("evidence_refs") or []
        target_id = evidence[0].get("id") if evidence else None
        await upsert_entry({
            "_id": remy_entry_id(signal["_id"]),
            "workspace_id": signal["workspace_id"],
            "lane": LANE_PASSIVE if outcome else LANE_ACTIVE,
            "visibility": "member",
            "member_user_id": signal["user_id"],
            "source": {"kind": "remy_signal", "id": signal["_id"], "type": signal.get("signal_type")},
            "actor": dict(actors.REMY),
            "category": CATEGORY_RECOMMENDATION,
            "title": _REMY_TITLES.get(signal.get("signal_type"), "Remy flagged something on your content"),
            "description": signal.get("member_message") or "",
            "target_id": target_id,
            "target_type": "Draft Post" if target_id else None,
            "target_label": await piece_label(target_id),
            "href": "/dashboard/remy",
            "status": "success" if outcome else "warning",
            "metadata": metadata,
            "decision": {"outcome": outcome} if outcome else None,
            "decided_at": _as_dt(signal.get("resolved_at")) if outcome else None,
            "snoozed_until": signal.get("snoozed_until"),
            "occurred_at": _as_dt(signal.get("created_at")),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("activity project_remy_signal failed for %s: %s", signal.get("_id"), exc)


# ─────────────────────────────────────────────────────────────────────────────
# Odette (workspace) → Active until decided, admin-only
# ─────────────────────────────────────────────────────────────────────────────

def odette_insight_entry_id(insight_id: str) -> str:
    return f"odette_insight:{insight_id}"


def odette_flag_entry_id(flag_id: str) -> str:
    return f"odette_flag:{flag_id}"


async def project_odette_insight(insight: dict) -> None:
    try:
        outcome = _INSIGHT_DECIDED.get(insight.get("status", "new"))
        metadata: dict[str, Any] = {"priority": _humanize(insight.get("priority"))}
        if outcome:
            metadata["decision"] = outcome.capitalize()
        description = insight.get("body_persona") or ""
        if insight.get("rationale"):
            description = f"{description}\n\nWhy: {insight['rationale']}" if description else insight["rationale"]
        await upsert_entry({
            "_id": odette_insight_entry_id(insight["_id"]),
            "workspace_id": insight["workspace_id"],
            "lane": LANE_PASSIVE if outcome else LANE_ACTIVE,
            "visibility": "admins",
            "source": {"kind": "odette_insight", "id": insight["_id"], "type": insight.get("kind")},
            "actor": dict(actors.ODETTE),
            "category": CATEGORY_RECOMMENDATION,
            "title": insight.get("title") or "Workspace recommendation",
            "href": "/dashboard/odette",
            "description": description,
            "status": "success" if outcome else "warning",
            "metadata": metadata,
            "decision": {"outcome": outcome} if outcome else None,
            "decided_at": _as_dt(insight.get("updated_at")) if outcome else None,
            "snoozed_until": insight.get("snoozed_until"),
            "occurred_at": _as_dt(insight.get("created_at")),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("activity project_odette_insight failed for %s: %s", insight.get("_id"), exc)


async def project_odette_flag(flag: dict) -> None:
    try:
        outcome = _FLAG_DECIDED.get(flag.get("status", "open"))
        # Resolved by the system itself (e.g. the connection recovered) —
        # history, but not a human decision.
        auto_resolved = bool(outcome) and flag.get("resolved_by") == "system"
        metric = flag.get("metric") or {}
        metadata: dict[str, Any] = {"severity": _humanize(flag.get("severity"))}
        if metric.get("name") and metric.get("limit"):
            metadata["signalMetric"] = f"{_humanize(metric['name'])}: {metric.get('value')} / {metric.get('limit')}"
        if auto_resolved:
            metadata["decision"] = "Resolved automatically"
        elif outcome:
            metadata["decision"] = outcome.capitalize()
        await upsert_entry({
            "_id": odette_flag_entry_id(flag["_id"]),
            "workspace_id": flag["workspace_id"],
            "lane": LANE_PASSIVE if outcome else LANE_ACTIVE,
            "visibility": "admins",
            "source": {"kind": "odette_flag", "id": flag["_id"], "type": flag.get("flag_type")},
            "actor": dict(actors.ODETTE),
            "category": CATEGORY_WORKSPACE_ALERT,
            "title": _FLAG_TITLES.get(flag.get("flag_type"), _humanize(flag.get("flag_type")) or "Workspace alert"),
            "description": flag.get("summary_persona") or "",
            "href": "/dashboard/odette",
            "status": "success" if outcome else ("failed" if flag.get("severity") == "critical" else "warning"),
            "metadata": metadata,
            "decision": {"outcome": outcome} if outcome and not auto_resolved else None,
            "decided_at": _as_dt(flag.get("resolved_at")) if outcome else None,
            "snoozed_until": flag.get("snoozed_until"),
            "occurred_at": _as_dt(flag.get("created_at")),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("activity project_odette_flag failed for %s: %s", flag.get("_id"), exc)


# ─────────────────────────────────────────────────────────────────────────────
# System jobs → Passive (autonomous work)
# ─────────────────────────────────────────────────────────────────────────────

async def record_system(
    *,
    workspace_id: str,
    key: str,
    actor_name: str,
    category: str,
    title: str,
    description: str,
    status: str = "success",
    channel: Optional[str] = None,
    target_id: Optional[str] = None,
    target_type: Optional[str] = None,
    href: Optional[str] = None,
    metadata: Optional[dict] = None,
    visibility: str = "workspace",
    occurred_at: Optional[datetime] = None,
    actor_user_id: Optional[str] = None,
    diff: Optional[dict] = None,
    restore: Optional[dict] = None,
    target_label: Optional[str] = None,
) -> None:
    """Record one piece of work that isn't a bus event — autonomous jobs
    (scheduler, retries, token refreshes) or an outcome with no event of its
    own (a failed publish). ``key`` makes it idempotent — the same key
    re-records (updates) the same row, e.g. one row per retry chain.
    ``actor_user_id`` attributes it to a member instead of ``actor_name``."""
    if not workspace_id:
        return
    actor = (
        await actors.member_actor(workspace_id, actor_user_id)
        if actor_user_id else actors.system_actor(actor_name)
    )
    await upsert_entry({
        "_id": f"system:{key}",
        "workspace_id": workspace_id,
        "lane": LANE_PASSIVE,
        "visibility": visibility,
        "source": {"kind": "system", "id": key, "type": category},
        "actor": actor,
        "category": category,
        "title": title,
        "description": description,
        "status": status,
        "channel": channel,
        "target_id": target_id,
        "target_type": target_type,
        "target_label": target_label or (
            await piece_label(target_id) if target_type in _PIECE_TARGETS else None
        ),
        "href": href,
        "metadata": metadata or None,
        "diff": diff,
        "restore": restore,
        "occurred_at": occurred_at or datetime.now(timezone.utc),
    })
