"""Ops Dashboard: the plain-language Activity feed and the Usage numbers for one platform, plus the RSS directory listings.

Activity gathers what happened to a platform from the records that already exist (staff actions, connection health,
failed publishes, Odette's flags) and says each one in a sentence a person can read and act on, with a tag for who
has to act: "Needs Ops", "Member action" or "FYI".
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.v1.ops_platform_lifecycle import _definition, _workspace_of
from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import (
    activity_entries,
    audio_assets,
    content_pieces,
    platform_listings,
    post_metrics,
    publish_incidents,
    workspace_connections,
    workspace_flags,
    workspaces,
)
from app.pipelines.platform_ops.events import record_platform_event
from app.platforms.base import PlatformDefinition

router = APIRouter()

ODETTE_FLAGS = ("platform_delivery_failing", "platform_capability_drift", "connection_broken")
FILTERS = {
    "all": None,
    "failures": ("failure",),
    "connections": ("connection",),
    "odette": ("odette",),
    "changes": ("change",),
}


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _names(definition: PlatformDefinition) -> list[str]:
    return [v for v in {definition.key, definition.label, definition.text_enum_value, definition.thread_enum_value} if v]


async def _workspace_names(ids: set[str]) -> dict[str, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    docs = await workspaces.find({"id": {"$in": list(ids)}}, {"id": 1, "name": 1}).to_list(length=len(ids))
    return {d["id"]: d.get("name", "") for d in docs}


# ── Activity ──────────────────────────────────────────────────────────────────

@router.get("/{key}/activity")
@limiter.limit("60/minute")
async def activity(
    request: Request,
    key: str,
    kind: str = Query("all", pattern="^(all|failures|connections|odette|changes)$"),
    user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    definition = _definition(key)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=30)
    events: list[dict[str, Any]] = []

    for row in await activity_entries.find(
        {"category": "platform_ops", "metadata.platform_key": key, "occurred_at": {"$gte": since}}
    ).sort("occurred_at", -1).to_list(length=60):
        events.append({
            "id": str(row["_id"]), "at": _aware(row.get("occurred_at")) or now, "kind": "change", "tone": "grey",
            "title": row.get("title", ""), "body": row.get("description", ""), "workspace_id": None, "tag": "FYI",
        })

    for row in await activity_entries.find(
        {"category": "account_connected", "channel": key, "occurred_at": {"$gte": since}}
    ).sort("occurred_at", -1).to_list(length=60):
        title = row.get("title", "")
        broken = "needs reconnecting" in title.lower()
        failing = "failed" in title.lower() or broken
        events.append({
            "id": str(row["_id"]), "at": _aware(row.get("occurred_at")) or now, "kind": "connection",
            "tone": "red" if broken else ("amber" if failing else "green"), "title": title,
            "body": row.get("description", ""), "workspace_id": row.get("workspace_id"),
            "tag": "Member action" if failing else "FYI",
        })

    for row in await publish_incidents.find(
        {"platform": key, "created_at": {"$gte": since}}
    ).sort("created_at", -1).to_list(length=60):
        error_type = row.get("error_type", "")
        events.append({
            "id": str(row["_id"]), "at": _aware(row.get("created_at")) or now, "kind": "failure",
            "tone": "red" if error_type == "FATAL" else "amber", "title": f"A post to {definition.label} failed",
            "body": row.get("error_message", "") or "The platform refused the post.", "workspace_id": row.get("workspace_id"),
            "tag": "Needs Ops" if error_type == "FATAL" else ("Member action" if error_type == "AUTH" else "FYI"),
        })

    for row in await workspace_flags.find(
        {"flag_type": {"$in": list(ODETTE_FLAGS)},
         "$or": [{"detail.platform": key}, {"detail.platforms.platform": key}]}
    ).sort("created_at", -1).to_list(length=30):
        events.append({
            "id": str(row["_id"]), "at": _aware(row.get("created_at")) or now, "kind": "odette",
            "tone": "amber", "title": "Odette raised a flag",
            "body": row.get("summary_persona") or row.get("flag_type", "").replace("_", " "),
            "workspace_id": row.get("workspace_id"), "tag": "Member action",
        })

    allowed = FILTERS[kind]
    if allowed:
        events = [e for e in events if e["kind"] in allowed]
    events.sort(key=lambda e: e["at"], reverse=True)
    events = events[:60]
    names = await _workspace_names({e["workspace_id"] for e in events if e["workspace_id"]})

    connections = await workspace_connections.find({"platform": key, "is_active": True}, {"health": 1, "expires_at": 1}).to_list(length=5000)
    broken = sum(1 for c in connections if (c.get("health") or {}).get("state") == "broken")
    soon = now + timedelta(days=7)
    expiring = sum(1 for c in connections if _aware(c.get("expires_at")) and now < _aware(c["expires_at"]) <= soon)
    failures = sum(1 for e in events if e["kind"] == "failure")
    summary = [
        f"{definition.label} | {now:%b} {now.day} | {len(connections)} connected",
        f"{broken} broken connection{'s' if broken != 1 else ''}, {expiring} expiring within 7 days",
        f"{failures} failed post{'s' if failures != 1 else ''} in the last 30 days",
        f"Odette flags: {sum(1 for e in events if e['kind'] == 'odette')}",
    ]
    return {
        "events": [
            {**e, "at": e["at"].isoformat(), "workspace_name": names.get(e["workspace_id"] or "", "All workspaces" if not e["workspace_id"] else "")}
            for e in events
        ],
        "summary": "\n".join(summary),
    }


# ── Usage ─────────────────────────────────────────────────────────────────────

@router.get("/{key}/usage")
@limiter.limit("60/minute")
async def usage(
    request: Request, key: str, days: int = Query(14, ge=7, le=30), user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    definition = _definition(key)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    month_ago = now - timedelta(days=30)

    published = await content_pieces.aggregate([
        {"$match": {"publish_target": key, "publish_status": "published", "published_at": {"$gte": since}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$published_at"}}, "n": {"$sum": 1}}},
    ]).to_list(length=60)
    per_day = {r["_id"]: r["n"] for r in published}
    series = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        series.append({"date": day, "published": per_day.get(day, 0)})

    published_30d = await content_pieces.count_documents({"publish_target": key, "publish_status": "published", "published_at": {"$gte": month_ago}})
    failed_30d = await content_pieces.count_documents({"publish_target": key, "publish_status": "failed", "updated_at": {"$gte": month_ago}})
    attempts = published_30d + failed_30d

    reasons = await publish_incidents.aggregate([
        {"$match": {"platform": key, "created_at": {"$gte": month_ago}}},
        {"$group": {"_id": "$error_type", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]).to_list(length=10)

    manual = await content_pieces.count_documents(
        {"published_manually": True, "platform": {"$in": _names(definition)}, "updated_at": {"$gte": month_ago}}
    )

    fetches = await post_metrics.find({"platform": key, "fetched_at": {"$gte": now - timedelta(days=7)}}, {"fetch_ok": 1}).to_list(length=500)
    if not fetches:
        analytics = "none"
    elif any(f.get("fetch_ok") is not False for f in fetches):
        analytics = "ok"
    else:
        analytics = "blocked"

    weeks = []
    connected_dates = [
        _aware(c.get("connected_at")) for c in await workspace_connections.find({"platform": key, "is_active": True}, {"connected_at": 1}).to_list(length=5000)
    ]
    connected_dates = [d for d in connected_dates if d]
    for back in range(7, -1, -1):
        edge = now - timedelta(weeks=back)
        weeks.append({"week_ending": edge.strftime("%Y-%m-%d"), "connected": sum(1 for d in connected_dates if d <= edge)})

    return {
        "days": days,
        "published_per_day": series,
        "published_30d": published_30d,
        "failed_30d": failed_30d,
        "success_rate": round(published_30d / attempts, 3) if attempts else None,
        "failures_by_reason": [{"reason": r["_id"] or "UNKNOWN", "count": r["n"]} for r in reasons],
        "manual_posts_30d": manual,
        "analytics_status": analytics,
        "average_latency_ms": None,
        "connected_over_time": weeks,
    }


# ── RSS directory listings ────────────────────────────────────────────────────

class ListingNote(BaseModel):
    note: str = Field("", max_length=500)


def _rss_only(definition: PlatformDefinition) -> None:
    if definition.integration_pattern != "rss_pull":
        raise HTTPException(status_code=400, detail={"code": "not_applicable", "message": "Only podcast directories have listings."})


@router.get("/{key}/listings")
@limiter.limit("60/minute")
async def listings(request: Request, key: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    definition = _definition(key)
    _rss_only(definition)
    docs = await platform_listings.find({"platform_key": key}).sort("updated_at", -1).to_list(length=5000)
    names = await _workspace_names({d["workspace_id"] for d in docs})
    with_feed = set(await audio_assets.distinct("workspace_id"))
    listed = {d["workspace_id"] for d in docs}
    rows = [{
        "workspace_id": d["workspace_id"],
        "workspace_name": names.get(d["workspace_id"], ""),
        "listing_url": d.get("listing_url", ""),
        "submitted_at": _aware(d.get("submitted_at")).isoformat() if _aware(d.get("submitted_at")) else None,
        "status": d.get("status", "submitted"),
        "note": d.get("note", ""),
    } for d in docs]
    return {
        "summary": {
            "submitted": sum(1 for r in rows if r["status"] == "submitted"),
            "live": sum(1 for r in rows if r["status"] == "live"),
            "rejected": sum(1 for r in rows if r["status"] == "rejected"),
            "no_listing": len(with_feed - listed),
        },
        "rows": rows,
    }


@router.put("/{key}/listings/{workspace_id}/note")
@limiter.limit("30/minute")
async def set_listing_note(
    request: Request, key: str, workspace_id: str, body: ListingNote, user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    definition = _definition(key)
    _rss_only(definition)
    result = await platform_listings.update_one(
        {"workspace_id": workspace_id, "platform_key": key},
        {"$set": {"note": body.note.strip(), "updated_at": datetime.now(timezone.utc)}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "That workspace has no listing for this directory."})
    await record_platform_event(
        event="listing.updated", definition=definition, actor_user_id=user["id"],
        actor_role="owner" if user.get("is_master_admin") else "staff", workspace_id=_workspace_of(user),
        description="A note was added to a directory listing.", subject_workspace_id=workspace_id,
    )
    return {"saved": True}
