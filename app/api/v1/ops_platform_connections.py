"""Ops Dashboard: who is connected to a platform, and the support actions on a connection.

For audits and support. Staff see workspace names, member names and roles, the account, health, token expiry and
recent results. They never see tokens or secrets. A member's email is hidden until staff choose to reveal it, and
every reveal is written to the Activity Log and rate limited. Only the Ops owner can force-disconnect a connection.

A connection is a row in workspace_connections for a platform that signs in, or a workspace's own settings row for
a webhook or manual-handoff platform.
"""

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.v1.ops_platform_lifecycle import _definition, _require_owner, _workspace_of
from app.core.config import settings
from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.db.mongo import (
    activity_entries,
    content_pieces,
    platform_configs,
    users,
    workspace_connections,
    workspace_members,
    workspaces,
)
from app.pipelines.platform_ops.events import record_platform_event
from app.pipelines.platform_ops.holds import hold_workspace_posts
from app.pipelines.publish.platform_config_store import PLATFORM_WIDE
from app.platforms.base import PlatformDefinition

router = APIRouter()
logger = logging.getLogger(__name__)

META_PLATFORMS = ("instagram", "facebook", "threads")
PAGE_LIMIT_MAX = 100


class RevealRequest(BaseModel):
    reason: str = Field("", max_length=300)


class ForceDisconnect(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


def _via(definition: PlatformDefinition) -> str:
    if definition.key in META_PLATFORMS:
        return "meta"
    if definition.key in ("youtube", "google"):
        return "google"
    if definition.integration_pattern == "token_webhook":
        return "webhook"
    if definition.integration_pattern == "manual_handoff":
        return "manual"
    return "direct"


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _status(health_state: str, expires_at: Optional[datetime], now: datetime) -> str:
    if health_state in ("degraded", "broken"):
        return health_state
    if expires_at and expires_at <= now + timedelta(days=7):
        return "expiring" if expires_at > now else "broken"
    return "healthy"


async def _raw_connections(definition: PlatformDefinition) -> list[dict[str, Any]]:
    if definition.integration_pattern in ("token_webhook", "manual_handoff"):
        docs = await platform_configs.find({"platform": definition.key, "workspace_id": {"$ne": PLATFORM_WIDE}}).to_list(length=5000)
        return [{
            "id": d["id"], "kind": "config", "workspace_id": d["workspace_id"],
            "account": d.get("label") or definition.label,
            "connected_by": d.get("created_by"), "connected_at": d.get("created_at"),
            "expires_at": None, "health": {"state": "healthy" if d.get("enabled", True) else "degraded", "reason": ""},
        } for d in docs]
    docs = await workspace_connections.find({"platform": definition.key, "is_active": True}).to_list(length=5000)
    return [{
        "id": d.get("id") or str(d["_id"]), "kind": "oauth", "workspace_id": d["workspace_id"],
        "account": d.get("username") or d.get("platform_user_id") or "",
        "connected_by": d.get("connected_by"), "connected_at": d.get("connected_at"),
        "expires_at": d.get("expires_at"), "health": d.get("health") or {}, "scopes": d.get("scopes"),
    } for d in docs]


async def _publish_stats(definition: PlatformDefinition) -> tuple[dict[str, int], dict[str, datetime], dict[str, int]]:
    since = datetime.now(timezone.utc) - timedelta(days=30)
    published = await content_pieces.aggregate([
        {"$match": {"publish_target": definition.key, "publish_status": "published", "published_at": {"$gte": since}}},
        {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}, "last": {"$max": "$published_at"}}},
    ]).to_list(length=5000)
    held = await content_pieces.aggregate([
        {"$match": {"hold.platform_key": definition.key}},
        {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}},
    ]).to_list(length=5000)
    return (
        {r["_id"]: r["n"] for r in published},
        {r["_id"]: _aware(r["last"]) for r in published if r.get("last")},
        {r["_id"]: r["n"] for r in held},
    )


async def _build_rows(definition: PlatformDefinition) -> list[dict[str, Any]]:
    raw = await _raw_connections(definition)
    if not raw:
        return []
    now = datetime.now(timezone.utc)
    workspace_ids = list({r["workspace_id"] for r in raw})
    names = {w["id"]: w.get("name", "") for w in await workspaces.find({"id": {"$in": workspace_ids}}, {"id": 1, "name": 1}).to_list(length=len(workspace_ids))}
    user_ids = list({r["connected_by"] for r in raw if r.get("connected_by")})
    people = {u["id"]: u.get("name", "") for u in await users.find({"id": {"$in": user_ids}}, {"id": 1, "name": 1}).to_list(length=len(user_ids) or 1)}
    roles = {
        (m["workspace_id"], m["user_id"]): m.get("role", "")
        for m in await workspace_members.find({"workspace_id": {"$in": workspace_ids}, "user_id": {"$in": user_ids}}, {"workspace_id": 1, "user_id": 1, "role": 1}).to_list(length=5000)
    }
    posts, last_publish, held = await _publish_stats(definition)
    via = _via(definition)

    rows = []
    for r in raw:
        expires = _aware(r["expires_at"])
        state = (r["health"] or {}).get("state", "healthy")
        status = _status(state, expires, now)
        rows.append({
            "id": r["id"],
            "kind": r["kind"],
            "workspace_id": r["workspace_id"],
            "workspace_name": names.get(r["workspace_id"], ""),
            "connected_by_name": people.get(r.get("connected_by") or "", ""),
            "connected_by_role": roles.get((r["workspace_id"], r.get("connected_by") or ""), ""),
            "account": r["account"],
            "via": via,
            "status": status,
            "health_reason": (r["health"] or {}).get("reason", ""),
            "ops_disconnected": (r["health"] or {}).get("reason") == "ops_disconnected",
            "token_expires_at": expires.isoformat() if expires else None,
            "days_left": (expires - now).days if expires else None,
            "connected_at": _aware(r.get("connected_at")).isoformat() if _aware(r.get("connected_at")) else None,
            "posts_30d": posts.get(r["workspace_id"], 0),
            "last_good_publish": last_publish[r["workspace_id"]].isoformat() if r["workspace_id"] in last_publish else None,
            "held_posts": held.get(r["workspace_id"], 0),
        })
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "connected": len(rows),
        "healthy": sum(1 for r in rows if r["status"] == "healthy"),
        "degraded": sum(1 for r in rows if r["status"] == "degraded"),
        "broken": sum(1 for r in rows if r["status"] == "broken"),
        "expiring": sum(1 for r in rows if r["status"] == "expiring"),
    }


def _filtered(rows: list[dict[str, Any]], q: str, status: str, via: str) -> list[dict[str, Any]]:
    needle = q.strip().lower()
    out = []
    for r in rows:
        if status and r["status"] != status:
            continue
        if via and r["via"] != via:
            continue
        if needle and needle not in " ".join([r["workspace_name"], r["connected_by_name"], r["account"]]).lower():
            continue
        out.append(r)
    return out


_STATUS_ORDER = {"broken": 0, "degraded": 1, "expiring": 2, "healthy": 3}


def _sorted(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort == "expires":
        return sorted(rows, key=lambda r: (r["token_expires_at"] is None, r["token_expires_at"] or ""))
    if sort == "posts":
        return sorted(rows, key=lambda r: -r["posts_30d"])
    if sort == "status":
        return sorted(rows, key=lambda r: (_STATUS_ORDER.get(r["status"], 9), r["workspace_name"].lower()))
    return sorted(rows, key=lambda r: r["workspace_name"].lower())


def _no_connections_tab(definition: PlatformDefinition) -> None:
    if definition.integration_pattern in ("rss_pull", "generation_only"):
        raise HTTPException(status_code=400, detail={"code": "not_applicable", "message": "This platform has no connections."})


@router.get("/{key}/connections")
@limiter.limit("60/minute")
async def list_connections(
    request: Request,
    key: str,
    q: str = Query("", max_length=100),
    status: str = Query("", pattern="^(healthy|degraded|broken|expiring)?$"),
    via: str = Query("", pattern="^(direct|meta|google|webhook|manual)?$"),
    sort: str = Query("workspace", pattern="^(workspace|expires|posts|status)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=PAGE_LIMIT_MAX),
    user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    definition = _definition(key)
    _no_connections_tab(definition)
    rows = await _build_rows(definition)
    matching = _sorted(_filtered(rows, q, status, via), sort)
    start = (page - 1) * limit
    return {
        "summary": _summary(rows),
        "rows": matching[start:start + limit],
        "total": len(matching),
        "page": page,
        "limit": limit,
        "can_force_disconnect": bool(user.get("is_master_admin")),
    }


async def _find_connection(definition: PlatformDefinition, connection_id: str) -> dict[str, Any]:
    for row in await _raw_connections(definition):
        if row["id"] == connection_id:
            return row
    raise HTTPException(status_code=404, detail={"code": "not_found", "message": "That connection was not found."})


def _sentence_tone(title: str, status: str) -> str:
    lowered = f"{title} {status}".lower()
    return "fail" if any(w in lowered for w in ("failed", "needs reconnecting", "expired", "broken", "disconnected")) else "info"


@router.get("/{key}/connections/{connection_id}/audit")
@limiter.limit("60/minute")
async def connection_audit(request: Request, key: str, connection_id: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    definition = _definition(key)
    _no_connections_tab(definition)
    conn = await _find_connection(definition, connection_id)
    workspace_id = conn["workspace_id"]
    now = datetime.now(timezone.utc)

    events: list[dict[str, Any]] = []
    connected_at = _aware(conn.get("connected_at"))
    if connected_at:
        who = (await users.find_one({"id": conn.get("connected_by")}, {"name": 1}) or {}).get("name") or "a member"
        events.append({"at": connected_at, "text": f"Connected by {who}.", "tone": "info"})

    for row in await activity_entries.find(
        {"workspace_id": workspace_id, "channel": key, "category": "account_connected"}
    ).sort("occurred_at", -1).to_list(length=30):
        text = row.get("title", "") + (f". {row['description']}" if row.get("description") else "")
        when = _aware(row.get("occurred_at")) or now
        events.append({"at": when, "text": text, "tone": _sentence_tone(row.get("title", ""), row.get("status", ""))})

    for row in await activity_entries.find(
        {"category": "platform_ops", "metadata.platform_key": key, "metadata.subject_workspace_id": workspace_id}
    ).sort("occurred_at", -1).to_list(length=20):
        events.append({"at": _aware(row.get("occurred_at")) or now, "text": row.get("description", ""), "tone": "fail" if "disconnected" in row.get("title", "") else "info"})

    since = now - timedelta(days=30)
    failed = await content_pieces.count_documents(
        {"workspace_id": workspace_id, "publish_target": key, "publish_status": "failed", "updated_at": {"$gte": since}}
    )
    events.sort(key=lambda e: e["at"], reverse=True)
    posts, last_publish, _held = await _publish_stats(definition)
    return {
        "events": [{**e, "at": e["at"].isoformat()} for e in events[:40]],
        "facts": {
            "via": _via(definition),
            "scopes": conn.get("scopes"),
            "last_good_publish": last_publish[workspace_id].isoformat() if workspace_id in last_publish else None,
            "failed_publishes_30d": failed,
            "secrets": "Encrypted, never shown",
        },
    }


async def _member_email(connection: dict[str, Any]) -> tuple[Optional[str], str]:
    member = await users.find_one({"id": connection.get("connected_by")}, {"email": 1, "name": 1}) or {}
    return member.get("email"), member.get("name") or "the member"


@router.post("/{key}/connections/{connection_id}/reveal-email")
@limiter.limit("30/hour")
async def reveal_email(
    request: Request, key: str, connection_id: str, body: RevealRequest, user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """Shows the email of the member who connected the account, once, and records that it was looked at."""
    definition = _definition(key)
    _no_connections_tab(definition)
    conn = await _find_connection(definition, connection_id)
    email, name = await _member_email(conn)
    if not email:
        raise HTTPException(status_code=404, detail={"code": "no_email", "message": "No email is on file for that member."})
    await record_platform_event(
        event="connection.email_revealed", definition=definition, actor_user_id=user["id"],
        actor_role="owner" if user.get("is_master_admin") else "staff", workspace_id=_workspace_of(user),
        description=f"The email of {name} was revealed." + (f" Reason: {body.reason.strip()}" if body.reason.strip() else ""),
        reason=body.reason.strip(), subject_workspace_id=conn["workspace_id"],
    )
    return {"email": email, "member": name}


async def _email_workspace_owner(definition: PlatformDefinition, workspace_id: str) -> bool:
    workspace = await workspaces.find_one({"id": workspace_id}, {"name": 1, "owner_id": 1}) or {}
    owner = await users.find_one({"id": workspace.get("owner_id")}, {"email": 1}) or {}
    if not owner.get("email"):
        return False
    await send_templated_email(
        "platform-reconnect-needed",
        owner["email"],
        {
            "PLATFORM": definition.label,
            "WORKSPACE_NAME": workspace.get("name", "your workspace"),
            "RECONNECT_URL": f"{settings.FRONTEND_URL}/dashboard/settings",
        },
    )
    return True


@router.post("/{key}/connections/{connection_id}/request-reconnect")
@limiter.limit("20/hour")
async def request_reconnect(request: Request, key: str, connection_id: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Emails the workspace owner asking them to reconnect the account."""
    definition = _definition(key)
    _no_connections_tab(definition)
    conn = await _find_connection(definition, connection_id)
    sent = await _email_workspace_owner(definition, conn["workspace_id"])
    if not sent:
        raise HTTPException(status_code=400, detail={"code": "no_owner_email", "message": "The workspace owner has no email on file."})
    await record_platform_event(
        event="connection.reconnect_requested", definition=definition, actor_user_id=user["id"],
        actor_role="owner" if user.get("is_master_admin") else "staff", workspace_id=_workspace_of(user),
        description="The workspace owner was emailed to reconnect.", subject_workspace_id=conn["workspace_id"],
    )
    return {"sent": True}


@router.post("/{key}/connections/{connection_id}/force-disconnect")
@limiter.limit("20/hour")
async def force_disconnect(
    request: Request, key: str, connection_id: str, body: ForceDisconnect, user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """Marks the connection broken so nothing uses it, holds that workspace's scheduled posts for the platform and tells
    the workspace owner. The stored token is not deleted: it stays unused until the member reconnects."""
    _require_owner(user)
    definition = _definition(key)
    conn = await _find_connection(definition, connection_id)
    if conn["kind"] != "oauth":
        raise HTTPException(
            status_code=400,
            detail={"code": "not_applicable", "message": "This platform has no sign in to disconnect. Switch its settings off instead."},
        )
    now = datetime.now(timezone.utc)
    await workspace_connections.update_one(
        {"workspace_id": conn["workspace_id"], "platform": key},
        {"$set": {
            "health.state": "broken",
            "health.reason": "ops_disconnected",
            "health.checked_at": now,
            "disconnected_by_ops": {"at": now, "by": user["id"], "reason": body.reason.strip()},
        }},
    )
    held = await hold_workspace_posts(definition, conn["workspace_id"])
    notified = await _email_workspace_owner(definition, conn["workspace_id"])
    await record_platform_event(
        event="connection.force_disconnected", definition=definition, actor_user_id=user["id"], actor_role="owner",
        workspace_id=_workspace_of(user),
        description=f"The {definition.label} connection was force disconnected. Reason: {body.reason.strip()}",
        reason=body.reason.strip(), subject_workspace_id=conn["workspace_id"],
    )
    return {"disconnected": True, "held_posts": held, "owner_notified": notified}


@router.get("/{key}/connections/export")
@limiter.limit("10/hour")
async def export_connections(request: Request, key: str, user: dict = Depends(require_platform_staff)) -> Response:
    """A CSV of the connections. Names, never emails, tokens or secrets."""
    definition = _definition(key)
    _no_connections_tab(definition)
    rows = await _build_rows(definition)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["workspace", "connected_by", "role", "account", "via", "status", "token_expires", "posts_30d", "last_good_publish"])
    for r in _sorted(rows, "workspace"):
        writer.writerow([
            r["workspace_name"], r["connected_by_name"], r["connected_by_role"], r["account"], r["via"], r["status"],
            r["token_expires_at"] or "", r["posts_30d"], r["last_good_publish"] or "",
        ])
    await record_platform_event(
        event="connections.exported", definition=definition, actor_user_id=user["id"],
        actor_role="owner" if user.get("is_master_admin") else "staff", workspace_id=_workspace_of(user),
        description=f"{len(rows)} connections were exported.",
    )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{definition.key}-connections.csv"'},
    )
