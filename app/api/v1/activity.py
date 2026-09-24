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
from app.db.mongo import (
    autonomy_trust,
    content_pieces,
    get_campaigns_collection,
    workspace_members,
    workspaces,
)
from app.shared.activity import live
from app.shared.activity import inbox as inbox_mod
from app.shared.activity.runs import list_runs
from app.shared.activity.store import (
    patch_entry,
    LANE_ACTIVE,
    LANE_PASSIVE,
    build_query,
    can_see_admin_rows,
    count_entries,
    get_entry,
    is_unread,
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


def _clean_description(doc: dict) -> str:
    """Rows projected before a post link moved out of the sentence carried it
    as a trailing raw URL — strip it; the link is rendered from href."""
    description = doc.get("description", "")
    href = doc.get("href")
    if href and description.endswith(" " + href):
        description = description[: -len(href) - 1]
    return description


def present(doc: dict, *, user_id: Optional[str] = None, read_before=None) -> dict:
    """Row → the frontend's ``ActivityLogEntry`` shape (plus lane/decision).
    ``timestamp`` / ``relativeTime`` are formatted client-side from
    ``occurredAt`` so they stay correct in the viewer's own timezone."""
    actor = doc.get("actor") or {}
    description = _clean_description(doc)
    out = {
        "id": doc["_id"],
        "occurredAt": _iso(doc.get("occurred_at")),
        "lane": doc.get("lane"),
        "actor": {k: actor[k] for k in ("name", "avatar", "type", "role", "agent") if actor.get(k)},
        "category": doc.get("category"),
        "title": doc.get("title", ""),
        "description": description,
        "status": doc.get("status", "success"),
    }
    optional = {
        "channel": doc.get("channel"),
        "targetId": doc.get("target_id"),
        "targetType": doc.get("target_type"),
        # Human label for the related item — never the raw id.
        "targetLabel": doc.get("target_label") or doc.get("subject"),
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
    if user_id:
        out["unread"] = is_unread(doc, user_id, read_before)
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
    cursor_read = await inbox_mod.read_before(ctx.workspace_id, ctx.user_id)
    return {
        "items": [present(d, user_id=ctx.user_id, read_before=cursor_read) for d in page["docs"]],
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
        "upcoming": await _upcoming(ctx.workspace_id),
        "completed": [_completed_card(d) for d in completed],
    }


_UPCOMING_WINDOW = timedelta(hours=24)
_UPCOMING_LIMIT = 5


async def _upcoming(workspace_id: str) -> list[dict]:
    """What's queued to happen on its own in the next 24h: scheduled posts
    (publish_scheduled_at is stored as an ISO string — the same comparison
    app.workers.scheduled_posts uses) and automated campaign runs."""
    now = datetime.now(timezone.utc)
    items: list[dict] = []
    async for piece in content_pieces.find(
        {
            "workspace_id": workspace_id,
            "publish_status": "queued",
            "deleted": {"$ne": True},
            "publish_scheduled_at": {"$lte": (now + _UPCOMING_WINDOW).isoformat()},
        },
        {"piece_id": 1, "content": 1, "platform": 1, "publish_target": 1, "publish_scheduled_at": 1},
    ).sort("publish_scheduled_at", 1).limit(_UPCOMING_LIMIT):
        first = ((piece.get("content") or "").strip().splitlines() or [""])[0]
        items.append({
            "id": f"post:{piece['piece_id']}",
            "kind": "scheduled_post",
            "title": first if len(first) <= 60 else first[:57].rstrip() + "…",
            "project": (piece.get("publish_target") or piece.get("platform") or "").capitalize(),
            "subtitle": "Scheduled post",
            "at": piece.get("publish_scheduled_at"),
            "href": "/dashboard/calendar",
        })
    async for campaign in get_campaigns_collection().find(
        {
            "workspace_id": workspace_id,
            "deleted": {"$ne": True},
            "status": {"$nin": ["paused", "completed"]},
            "cadence.frequency": {"$ne": "manual"},
            "cadence.next_run_at": {"$lte": now + _UPCOMING_WINDOW},
        },
        {"id": 1, "name": 1, "cadence": 1},
    ).sort("cadence.next_run_at", 1).limit(_UPCOMING_LIMIT):
        items.append({
            "id": f"campaign:{campaign['id']}",
            "kind": "campaign_run",
            "title": campaign.get("name") or "Campaign",
            "project": f"{(campaign.get('cadence') or {}).get('frequency', '').capitalize()} campaign",
            "subtitle": "Next batch",
            "at": _iso((campaign.get("cadence") or {}).get("next_run_at")),
            "href": "/dashboard/campaigns",
        })
    items.sort(key=lambda i: i["at"] or "")
    return items[:_UPCOMING_LIMIT]


# ── Inbox ───────────────────────────────────────────────────────────────────

def _inbox_item(doc: dict, unread: bool) -> dict:
    """Row → the Inbox popover's card. ``type`` drives its icon colour:
    success (done), info (needs a decision), sync (routine automation),
    failed (something broke)."""
    status = doc.get("status")
    if status == "failed":
        kind = "failed"
    elif doc.get("lane") == "active":
        kind = "info"
    elif (doc.get("actor") or {}).get("type") in ("system_cron", "webhook"):
        kind = "sync"
    else:
        kind = "success"
    return {
        "id": doc["_id"],
        "title": doc.get("title", ""),
        "desc": _clean_description(doc),
        "occurredAt": _iso(doc.get("occurred_at")),
        "type": kind,
        "category": doc.get("category"),
        "lane": doc.get("lane"),
        "href": doc.get("href"),
        "unread": unread,
    }


@router.get("/inbox")
@limiter.limit("60/minute")
async def get_inbox(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    page = await inbox_mod.list_inbox(ctx.workspace_id, ctx.user_id, ctx.role)
    return {
        "items": [_inbox_item(d, d["_id"] in page["unread_ids"]) for d in page["docs"]],
        "unread": page["unread"],
    }


class InboxReadBody(BaseModel):
    ids: Optional[list[str]] = Field(None, max_length=100)


@router.post("/inbox/read")
@limiter.limit("60/minute")
async def read_inbox(
    request: Request,
    body: InboxReadBody,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Mark the given items read, or everything when ``ids`` is omitted."""
    if body.ids:
        await inbox_mod.set_read(ctx.workspace_id, ctx.user_id, body.ids)
    else:
        await inbox_mod.mark_all_read(ctx.workspace_id, ctx.user_id)
    return {"ok": True}


# ── Read / unread / delete (Activity Log rows) ──────────────────────────────

class ReadBody(BaseModel):
    #: Omitted → mark everything read.
    ids: Optional[list[str]] = Field(None, max_length=100)
    unread: bool = False


@router.post("/read")
@limiter.limit("120/minute")
async def mark_read(
    request: Request,
    body: ReadBody,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    if body.ids:
        await inbox_mod.set_read(ctx.workspace_id, ctx.user_id, body.ids, unread=body.unread)
    elif body.unread:
        raise HTTPException(status_code=400, detail="Pick the items to mark unread.")
    else:
        await inbox_mod.mark_all_read(ctx.workspace_id, ctx.user_id)
    return {"ok": True, "unread": await inbox_mod.unread_count(ctx.workspace_id, ctx.user_id, ctx.role)}


class HideBody(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=100)


@router.post("/hide")
@limiter.limit("60/minute")
async def hide_entries(
    request: Request,
    body: HideBody,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Delete rows from the caller's own log. The shared audit trail is
    untouched; items still waiting on a decision are skipped (dismiss them)."""
    removed = await inbox_mod.hide(ctx.workspace_id, ctx.user_id, body.ids)
    return {"removed": removed, "skipped": len(body.ids) - removed}


@router.get("/unread-count")
@limiter.limit("120/minute")
async def unread_count(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The sidebar badge — unread items that need attention (the Inbox set)."""
    return {"unread": await inbox_mod.unread_count(ctx.workspace_id, ctx.user_id, ctx.role)}


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
    return present(updated, user_id=ctx.user_id) if updated else {"id": entry_id}


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
    cursor_read = await inbox_mod.read_before(workspace_id, user_id)

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
                yield f"event: activity\ndata: {json.dumps(present(row, user_id=user_id, read_before=cursor_read))}\n\n"
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
