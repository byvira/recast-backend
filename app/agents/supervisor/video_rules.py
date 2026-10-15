"""Odette's video rules: deterministic, no model. Each returns zero or more flag descriptors for one workspace, built with the same
`flag` helper the other rules use. Reads the render jobs, the plan's limits, automatic recipes and scheduled posts that carry a video."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from app.agents.supervisor import thresholds as T
from app.db.mongo import content_pieces, get_db, workspace_connections, workspace_events

Flag = Callable[[str, str, dict, dict], Awaitable[dict]]

VIDEO_FAILURES_24H = 3            # failed renders in the window before Odette says so
VOICE_CHANGE_DAYS = 7                # how far back a brand voice change still counts against a waiting video
SCHEDULED_LOOKAHEAD_HOURS = 48    # how far ahead a scheduled video post is checked against its connection


def _aware(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value if isinstance(value, datetime) else None


async def evaluate(workspace_id: str, now: datetime, flag: Flag) -> list[dict]:
    from app.pipelines.publish.spine import platform_key
    from app.video import limits

    db = get_db()
    out: list[dict] = []
    since = now - timedelta(hours=T.RULE_LOOKBACK_HOURS)

    # 1. repeated failed renders
    failed = await db["video_jobs"].count_documents({
        "workspace_id": workspace_id, "created_at": {"$gte": since}, "status": {"$in": ["failed", "blocked"]}, "kind": {"$in": [None, "render"]},
    })
    if failed >= VIDEO_FAILURES_24H:
        out.append(await flag("video_renders_failing", "warning", {"failures": failed, "window_hours": T.RULE_LOOKBACK_HOURS},
                              {"name": "failed_renders_24h", "value": float(failed), "limit": float(VIDEO_FAILURES_24H)}))

    # 2 and 3. render minutes and storage near the plan's limit
    meters = await limits.usage(workspace_id)
    minutes = meters["render_minutes"]
    if minutes["state"] != "ok":
        out.append(await flag("video_minutes_high", "critical" if minutes["state"] == "over" else "warning",
                              {"used": round(minutes["used"]), "limit": minutes["limit"]},
                              {"name": "render_minutes_month", "value": float(minutes["used"]), "limit": float(minutes["limit"])}))
    storage = meters["storage_mb"]
    if storage["state"] != "ok":
        out.append(await flag("video_storage_high", "critical" if storage["state"] == "over" else "warning",
                              {"used": round(storage["used"]), "limit": storage["limit"]},
                              {"name": "storage_mb", "value": float(storage["used"]), "limit": float(storage["limit"])}))

    # 4. an automatic recipe that stopped on a failed check and is still waiting
    stopped = await db["video_recipes"].find_one({"workspace_id": workspace_id, "automation.paused": True}, {"name": 1, "automation": 1})
    if stopped:
        out.append(await flag("video_automation_stopped", "warning",
                              {"recipe": stopped.get("name"), "reason": (stopped["automation"].get("paused_reason") or "a check failed")[:160]},
                              {"name": "paused_automations", "value": 1.0, "limit": 0.0}))

    # 4b. a recipe that publishes without a person's sign-off
    auto = await db["video_recipes"].count_documents({"workspace_id": workspace_id, "automation.enabled": True, "automation.autonomy": "auto_publish"})
    if auto:
        out.append(await flag("video_auto_publish_on", "warning", {"count": auto, "noun": "recipe" if auto == 1 else "recipes"},
                              {"name": "auto_publish_recipes", "value": float(auto), "limit": 0.0}))

    # 5. a scheduled post with a video whose connection runs out first
    soon = now + timedelta(hours=SCHEDULED_LOOKAHEAD_HOURS)
    pieces = await content_pieces.find({
        "workspace_id": workspace_id, "deleted": {"$ne": True}, "publish_status": {"$in": ["queued", "pending"]},
        "publish_scheduled_at": {"$gte": now, "$lte": soon}, "attachments.asset_type": {"$in": ["project", "video"]},
    }, {"platform": 1, "publish_scheduled_at": 1}).to_list(length=200)
    if pieces:
        connections = await workspace_connections.find({"workspace_id": workspace_id, "is_active": True}, {"platform": 1, "expires_at": 1}).to_list(length=100)
        expiry = {platform_key(c.get("platform", "")): _aware(c.get("expires_at")) for c in connections}
        at_risk: dict[str, int] = {}
        for p in pieces:
            key = platform_key(p.get("platform", ""))
            ends, goes = expiry.get(key), _aware(p.get("publish_scheduled_at"))
            if ends and goes and ends < goes:
                at_risk[p.get("platform", key)] = at_risk.get(p.get("platform", key), 0) + 1
        for platform, count in at_risk.items():
            out.append(await flag("video_connection_expiring", "warning",
                                  {"platform": platform, "count": count, "noun": "post" if count == 1 else "posts"},
                                  {"name": "scheduled_videos_at_risk", "value": float(count), "limit": 0.0}))

    # 6. a scheduled video made before the brand voice last changed
    window = now - timedelta(days=VOICE_CHANGE_DAYS)
    changes = await workspace_events.find(
        {"workspace_id": workspace_id, "event_type": "brand.voice_updated", "occurred_at": {"$gte": window.isoformat()}},
        {"occurred_at": 1, "payload.brand_id": 1},
    ).to_list(length=200)
    latest: dict[str, datetime] = {}
    for e in changes:
        when, brand = _aware(e.get("occurred_at")), (e.get("payload") or {}).get("brand_id")
        if when and brand and (brand not in latest or when > latest[brand]):
            latest[brand] = when
    if latest:
        waiting = await content_pieces.find({
            "workspace_id": workspace_id, "deleted": {"$ne": True}, "publish_status": {"$in": ["queued", "pending"]},
            "brand_id": {"$in": list(latest)}, "attachments.asset_type": {"$in": ["project", "video"]},
        }, {"brand_id": 1, "created_at": 1}).to_list(length=200)
        stale = [p for p in waiting if (_aware(p.get("created_at")) or now) < latest[p["brand_id"]]]
        if stale:
            out.append(await flag("video_voice_changed", "warning", {"count": len(stale), "noun": "post" if len(stale) == 1 else "posts"},
                                  {"name": "videos_before_voice_change", "value": float(len(stale)), "limit": 0.0}))
    return out
