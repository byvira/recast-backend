"""Service layer for the ``/supervisor/*`` routes (admin/owner only).

Every function is workspace-scoped by an id from the authenticated context.
Insights, flags and notifications are visible only to workspace admins/owners —
that gate is enforced in the route layer via ``require("view_workspace_insights")``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.agents.supervisor.notify import resolve_admin_user_ids
from app.core.config import settings
from app.db.mongo import (
    admin_notifications,
    personal_signals,
    workspace_flags,
    workspace_insights,
    workspace_members,
    workspaces,
)

logger = logging.getLogger(__name__)

_INSIGHT_STATUSES = {"new", "seen", "dismissed", "actioned"}
_FLAG_STATUSES = {"resolved", "muted", "open"}

_arq_pool = None


# ── insights ────────────────────────────────────────────────────────────────

async def list_insights(workspace_id: str, *, status: str | None = None, limit: int = 50) -> dict:
    q: dict = {"workspace_id": workspace_id}
    if status:
        q["status"] = status
    rows = await workspace_insights.find(q).sort("created_at", -1).limit(min(limit, 200)).to_list(200)
    for r in rows:
        r["id"] = r.pop("_id")
    return {"items": rows, "total": len(rows)}


async def set_insight_status(workspace_id: str, insight_id: str, status: str) -> dict:
    if status not in _INSIGHT_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(_INSIGHT_STATUSES)}")
    res = await workspace_insights.update_one(
        {"_id": insight_id, "workspace_id": workspace_id},
        {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Insight not found.")
    return {"id": insight_id, "status": status}


# ── flags ───────────────────────────────────────────────────────────────────

async def list_flags(workspace_id: str, *, status: str | None = "open", limit: int = 50) -> dict:
    q: dict = {"workspace_id": workspace_id}
    if status:
        q["status"] = status
    rows = await workspace_flags.find(q).sort("created_at", -1).limit(min(limit, 200)).to_list(200)
    for r in rows:
        r["id"] = r.pop("_id")
    return {"items": rows, "total": len(rows)}


async def set_flag_status(workspace_id: str, flag_id: str, status: str) -> dict:
    if status not in {"resolved", "muted"}:
        raise HTTPException(status_code=400, detail="status must be 'resolved' or 'muted'")
    res = await workspace_flags.update_one(
        {"_id": flag_id, "workspace_id": workspace_id},
        {"$set": {"status": status, "resolved_at": datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Flag not found.")
    return {"id": flag_id, "status": status}


# ── dashboard ───────────────────────────────────────────────────────────────

async def dashboard(workspace_id: str) -> dict:
    now = datetime.now(timezone.utc)
    ws = await workspaces.find_one({"id": workspace_id}) or {}
    seats = int((ws.get("tier_config") or {}).get("seats", 0) or 0)
    active_members = await workspace_members.count_documents(
        {"workspace_id": workspace_id, "status": "active"}
    )

    open_flags = await workspace_flags.find(
        {"workspace_id": workspace_id, "status": "open"}
    ).sort("created_at", -1).to_list(100)
    flags_by_severity: dict[str, int] = {}
    for f in open_flags:
        flags_by_severity[f.get("severity", "warning")] = flags_by_severity.get(f.get("severity", "warning"), 0) + 1

    top_insights = await workspace_insights.find(
        {"workspace_id": workspace_id, "status": {"$in": ["new", "seen"]}}
    ).sort([("priority", -1), ("created_at", -1)]).limit(5).to_list(5)
    for i in top_insights:
        i["id"] = i.pop("_id")

    since = now - timedelta(days=7)
    sig_rows = await personal_signals.find(
        {"workspace_id": workspace_id, "created_at": {"$gte": since}}
    ).to_list(2000)
    sig_by_member: dict[str, dict] = {}
    for s in sig_rows:
        m = sig_by_member.setdefault(s.get("user_id", "?"), {})
        m[s.get("signal_type", "?")] = m.get(s.get("signal_type", "?"), 0) + 1

    unread_notifs = await admin_notifications.count_documents({"workspace_id": workspace_id})

    return {
        "workspace_id": workspace_id,
        "tier": ws.get("tier"),
        "seats": seats,
        "active_members": active_members,
        "seat_headroom": (seats - active_members) if seats else None,
        "open_flags": [
            {"id": f["_id"], "type": f.get("flag_type"), "severity": f.get("severity"),
             "detection": f.get("detection"), "summary": f.get("summary_persona"),
             "created_at": str(f.get("created_at"))}
            for f in open_flags
        ],
        "open_flags_by_severity": flags_by_severity,
        "top_insights": top_insights,
        "assistant_signals_7d_by_member": sig_by_member,
        "notifications_total": unread_notifs,
        "generated_at": now.isoformat(),
    }


# ── notifications ───────────────────────────────────────────────────────────

async def list_notifications(workspace_id: str, user_id: str, *, limit: int = 50) -> dict:
    rows = await admin_notifications.find(
        {"workspace_id": workspace_id, "audience": "admins"}
    ).sort("created_at", -1).limit(min(limit, 200)).to_list(200)
    items = []
    for r in rows:
        items.append({
            "id": r["_id"],
            "title": r.get("title"),
            "body_persona": r.get("body_persona"),
            "source": r.get("source"),
            "severity": r.get("severity"),
            "created_at": str(r.get("created_at")),
            "read": user_id in (r.get("read_by") or []),
        })
    return {"items": items, "total": len(items),
            "unread": sum(1 for i in items if not i["read"])}


async def mark_notification_read(workspace_id: str, user_id: str, notif_id: str) -> dict:
    res = await admin_notifications.update_one(
        {"_id": notif_id, "workspace_id": workspace_id},
        {"$addToSet": {"read_by": user_id}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found.")
    return {"id": notif_id, "read": True}


# ── on-demand run ──────────────────────────────────────────────────────────

async def trigger_run(workspace_id: str) -> dict:
    """Enqueue a supervisor reasoning pass on the arq worker. Falls back to an
    in-process background task if the queue is unreachable."""
    global _arq_pool
    try:
        if _arq_pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings
            _arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        job = await _arq_pool.enqueue_job("run_supervisor_now", workspace_id)
        return {"status": "enqueued", "job_id": getattr(job, "job_id", None)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("trigger_run: arq enqueue failed (%s) — running in-process", exc)
        from app.agents.supervisor.ticks import run_supervisor_now
        asyncio.create_task(run_supervisor_now({}, workspace_id))
        return {"status": "started_inline"}


async def close_arq_pool() -> None:
    """Release the enqueue pool (app shutdown / tests)."""
    global _arq_pool
    if _arq_pool is not None:
        try:
            await _arq_pool.aclose()
        except Exception:  # noqa: BLE001
            pass
        _arq_pool = None
