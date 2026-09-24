"""Activity Log API — two lanes over the ``activity_entries`` read model.

* ``GET  /activity``                 — one lane, filtered, cursor-paginated
* ``POST /activity/{id}/decision``   — accept / dismiss / snooze an Active item
* ``GET  /activity/stream``          — live rows over SSE

Every route resolves the caller's workspace membership first; row visibility
(workspace / admins / member-private) is applied in the query itself, so a
member can never page into Odette's admin items or another member's Remy
feedback. Decisions are delegated to the owning agent's service, so the
Remy and Odette pages and the Activity Log always agree.
"""


import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents.personal import service as remy_service
from app.agents.supervisor import service as odette_service
from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import autonomy_trust, workspace_members, workspaces
from app.shared.activity import live
from app.shared.activity.runs import list_runs
from app.shared.activity.store import (
    patch_entry,
    LANE_ACTIVE,
    LANE_PASSIVE,
    build_query,
    can_see_admin_rows,
    count_entries,
    get_entry,
    is_visible_to,
    list_entries,
    visibility_filter,
)
from app.db.mongo import activity_entries

router = APIRouter()
logger = logging.getLogger(__name__)

_KEEPALIVE_SECONDS = 25


def _iso(value) -> Optional[str]:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return value


def present(doc: dict) -> dict:
    """Row → the frontend's ``ActivityLogEntry`` shape (plus lane/decision).
    ``timestamp`` / ``relativeTime`` are formatted client-side from
    ``occurredAt`` so they stay correct in the viewer's own timezone."""
    actor = doc.get("actor") or {}
    out = {
        "id": doc["_id"],
        "occurredAt": _iso(doc.get("occurred_at")),
        "lane": doc.get("lane"),
        "actor": {k: actor[k] for k in ("name", "avatar", "type", "role", "agent") if actor.get(k)},
        "category": doc.get("category"),
        "title": doc.get("title", ""),
        "description": doc.get("description", ""),
        "status": doc.get("status", "success"),
    }
    optional = {
        "channel": doc.get("channel"),
        "targetId": doc.get("target_id"),
        "targetType": doc.get("target_type"),
        "href": doc.get("href"),
        "diff": doc.get("diff"),
        "metadata": doc.get("metadata"),
        "decision": (doc.get("decision") or {}).get("outcome"),
        "snoozedUntil": _iso(doc.get("snoozed_until")),
        "source": (doc.get("source") or {}).get("kind"),
        "restore": (
            {"pieceId": doc["restore"]["piece_id"], "versionNumber": doc["restore"]["version_number"]}
            if doc.get("restore") else None
        ),
    }
    out.update({k: v for k, v in optional.items() if v})
    return out


@router.get("")
@limiter.limit("60/minute")
async def list_activity(
    request: Request,
    lane: Literal["active", "passive"] = Query("passive"),
    category: Optional[str] = Query(None, max_length=40),
    actor_type: Optional[Literal["ai_agent", "user", "team_member", "system_cron"]] = Query(None),
    status: Optional[Literal["success", "warning", "failed"]] = Query(None),
    q: Optional[str] = Query(None, max_length=120),
    cursor: Optional[str] = Query(None, max_length=120),
    limit: int = Query(30, ge=1, le=100),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    query = build_query(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, role=ctx.role,
        lane=lane, category=category, actor_type=actor_type, status=status, q=q,
    )
    try:
        page = await list_entries(query, cursor=cursor, limit=limit)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid cursor.")
    # Totals are only needed on the first page; later pages reuse the header.
    total = await count_entries(query) if not cursor else None
    active_total = None
    if not cursor:
        active_total = (
            total if lane == LANE_ACTIVE else await count_entries(build_query(
                workspace_id=ctx.workspace_id, user_id=ctx.user_id, role=ctx.role, lane=LANE_ACTIVE,
            ))
        )
    return {
        "items": [present(d) for d in page["docs"]],
        "next_cursor": page["next_cursor"],
        "total": total,
        "active_total": active_total,
    }


_COMPLETED_WINDOW = timedelta(hours=24)
_COMPLETED_LIMIT = 5


def _completed_card(doc: dict) -> dict:
    """Row → the Control Tower's "Recently Completed" card."""
    metadata = doc.get("metadata") or {}
    if doc.get("category") == "content_generated":
        pieces = metadata.get("branchesCount") or 0
        return {
            "id": doc["_id"],
            "kind": "generated",
            "title": doc.get("subject") or doc.get("title", ""),
            "project": ", ".join(doc.get("platforms") or []),
            "subtitle": f"Generated {pieces} {'output' if pieces == 1 else 'outputs'}",
            "occurredAt": _iso(doc.get("occurred_at")),
            "href": doc.get("href"),
        }
    return {
        "id": doc["_id"],
        "kind": "published",
        "title": doc.get("title", ""),
        "project": (doc.get("actor") or {}).get("name", ""),
        "subtitle": "Live on platform",
        "channel": doc.get("channel"),
        "occurredAt": _iso(doc.get("occurred_at")),
        "href": doc.get("href"),
    }


@router.get("/control-tower")
@limiter.limit("60/minute")
async def control_tower(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Live work only — what's running now and what just finished. Failures
    and agent suggestions live in the Activity Log, not here."""
    query = visibility_filter(ctx.workspace_id, ctx.user_id, ctx.role)
    query.update({
        "lane": LANE_PASSIVE,
        "status": "success",
        "category": {"$in": ["content_generated", "post_published"]},
        "occurred_at": {"$gte": datetime.now(timezone.utc) - _COMPLETED_WINDOW},
    })
    completed = await activity_entries.find(query).sort(
        [("occurred_at", -1), ("_id", -1)]
    ).limit(_COMPLETED_LIMIT).to_list(length=_COMPLETED_LIMIT)
    return {
        "live": await list_runs(ctx.workspace_id),
        "completed": [_completed_card(d) for d in completed],
    }


class DecisionBody(BaseModel):
    decision: Literal["accept", "dismiss", "snooze"]
    snooze_hours: int = Field(24, ge=1, le=24 * 14)


@router.post("/{entry_id}/decision")
@limiter.limit("60/minute")
async def decide(
    request: Request,
    entry_id: str,
    body: DecisionBody,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    doc = await get_entry(entry_id)
    if not doc or doc.get("workspace_id") != ctx.workspace_id or not is_visible_to(doc, ctx.user_id, ctx.role):
        raise HTTPException(status_code=404, detail="Activity item not found.")
    if doc.get("lane") != LANE_ACTIVE:
        raise HTTPException(status_code=409, detail="This item has already been decided.")

    source = doc.get("source") or {}
    kind, source_id = source.get("kind"), source.get("id")
    until = datetime.now(timezone.utc) + timedelta(hours=body.snooze_hours)

    if kind == "remy_signal":
        if body.decision == "snooze":
            await remy_service.snooze_signal(ctx.workspace_id, ctx.user_id, source_id, until)
        else:
            await remy_service.resolve_signal(
                ctx.workspace_id, ctx.user_id, source_id,
                "acknowledged" if body.decision == "accept" else "dismissed",
            )
    elif kind in ("odette_insight", "odette_flag"):
        if not can_see_admin_rows(ctx.role):
            raise HTTPException(status_code=403, detail="Only workspace admins can decide on Odette's items.")
        if kind == "odette_insight":
            if body.decision == "snooze":
                await odette_service.snooze_insight(ctx.workspace_id, source_id, until)
            else:
                await odette_service.set_insight_status(
                    ctx.workspace_id, source_id,
                    "actioned" if body.decision == "accept" else "dismissed",
                )
        else:
            if body.decision == "snooze":
                await odette_service.snooze_flag(ctx.workspace_id, source_id, until)
            else:
                await odette_service.set_flag_status(
                    ctx.workspace_id, source_id,
                    "resolved" if body.decision == "accept" else "muted",
                )
    elif kind == "next_step":
        # No underlying agent collection — the row is the record.
        if body.decision == "snooze":
            await patch_entry(entry_id, {"snoozed_until": until})
        else:
            outcome = "accepted" if body.decision == "accept" else "dismissed"
            await patch_entry(entry_id, {
                "lane": LANE_PASSIVE,
                "status": "success",
                "decision": {"outcome": outcome},
                "decided_at": datetime.now(timezone.utc),
                "metadata": {**(doc.get("metadata") or {}), "decision": outcome.capitalize()},
            })
    else:
        raise HTTPException(status_code=400, detail="This item doesn't take a decision.")

    updated = await get_entry(entry_id)
    return present(updated) if updated else {"id": entry_id}


@router.get("/autonomy")
@limiter.limit("30/minute")
async def autonomy_scores(
    request: Request,
    ctx: WorkspaceContext = Depends(require("view_workspace_insights")),
) -> dict:
    """Shadow-mode trust scores per platform — read-only evidence. Nothing
    auto-publishes; see app.agents.feedback.trust."""
    items = []
    async for doc in autonomy_trust.find({"workspace_id": ctx.workspace_id}).sort("platform", 1):
        shadow = doc.get("shadow") or {}
        items.append({
            "platform": doc.get("platform"),
            "pipelineType": doc.get("pipeline_type", "text"),
            "score": doc.get("score"),
            "threshold": doc.get("threshold"),
            "eligible": bool(doc.get("eligible")),
            "components": doc.get("components") or {},
            "decided": doc.get("decided", 0),
            "shadow": {
                "decisions": shadow.get("total", 0),
                "agreement": round(shadow["agree"] / shadow["total"], 3) if shadow.get("total") else None,
            },
            "computedAt": _iso(doc.get("computed_at")),
        })
    return {"items": items, "mode": "shadow"}


class ThresholdBody(BaseModel):
    platform: str = Field(..., min_length=1, max_length=40)
    threshold: int = Field(..., ge=0, le=100)


@router.put("/autonomy/threshold")
@limiter.limit("20/minute")
async def set_autonomy_threshold(
    request: Request,
    body: ThresholdBody,
    ctx: WorkspaceContext = Depends(require("manage_workspace_settings")),
) -> dict:
    await workspaces.update_one(
        {"id": ctx.workspace_id},
        {"$set": {f"autonomy.thresholds.{body.platform}": body.threshold}},
    )
    from app.agents.feedback.trust import refresh_tuple
    return await refresh_tuple(ctx.workspace_id, body.platform)


@router.get("/stream")
async def stream_activity(
    request: Request,
    workspace_id: str = Query(..., max_length=64),
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Live rows for one workspace. ``workspace_id`` is a query param because
    a browser ``EventSource`` can't send the ``X-Workspace-Id`` header; the
    same active-membership check ``get_current_workspace`` does runs here."""
    member = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": current_user["id"]}
    )
    if not member or member.get("status") != "active":
        raise HTTPException(status_code=403, detail="Not a member of this workspace.")
    user_id, role = current_user["id"], member.get("role", "")

    async def events():
        q = live.subscribe(workspace_id)
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected() or live.is_dropped(workspace_id, q):
                    break
                try:
                    row = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                if not is_visible_to(row, user_id, role):
                    continue
                for key in ("occurred_at", "snoozed_until"):
                    if isinstance(row.get(key), str):
                        row[key] = datetime.fromisoformat(row[key])
                yield f"event: activity\ndata: {json.dumps(present(row))}\n\n"
        finally:
            live.unsubscribe(workspace_id, q)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
