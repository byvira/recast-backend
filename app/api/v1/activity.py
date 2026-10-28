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
from urllib.parse import urlparse

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
from app.pipelines.publish.spine import iso_utc, to_utc_datetime
from app.shared.activity import live
from app.shared.activity import inbox as inbox_mod
from app.shared import pipeline_runs
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
    hidden_flags_for,
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


def _aware(value: datetime) -> datetime:
    """A date sent without a timezone is read as UTC, the same as every stored time."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _clean_description(doc: dict) -> str:
    """Rows projected before a post link moved out of the sentence carried it
    as a trailing raw URL — strip it; the link is rendered from href."""
    description = doc.get("description", "")
    href = doc.get("href")
    if href and description.endswith(" " + href):
        description = description[: -len(href) - 1]
    return description


def _scalar_metadata(metadata: Optional[dict]) -> Optional[dict]:
    """The screen reads metadata as plain text and numbers only. Empty values are dropped and true or false become words, so
    one row with an empty field never makes the whole page fail to load."""
    if not isinstance(metadata, dict):
        return None
    clean: dict = {}
    for key, value in metadata.items():
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        clean[str(key)] = ("true" if value else "false") if isinstance(value, bool) else value if isinstance(value, (str, int, float)) else str(value)
    return clean or None


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
        "metadata": _scalar_metadata(doc.get("metadata")),
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
    occurred_from: Optional[datetime] = Query(None, alias="from", description="Only entries at or after this moment."),
    occurred_to: Optional[datetime] = Query(None, alias="to", description="Only entries before this moment."),
    unread_only: bool = Query(False),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    if occurred_from and occurred_to and _aware(occurred_from) >= _aware(occurred_to):
        raise HTTPException(status_code=400, detail="The start of the date range must be before its end.")
    # Reads that do not depend on each other go out together, since each is a round trip to the database.
    hidden_flags, cursor_read = await asyncio.gather(
        hidden_flags_for(ctx.workspace_id), inbox_mod.read_before(ctx.workspace_id, ctx.user_id),
    )
    query = build_query(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, role=ctx.role,
        lane=lane, category=category, actor_type=actor_type, status=status, q=q, hidden_flags=hidden_flags,
        occurred_from=_aware(occurred_from) if occurred_from else None, occurred_to=_aware(occurred_to) if occurred_to else None,
        unread_only=unread_only, read_before=cursor_read,
    )

    async def _none() -> None:
        return None

    # Totals are only needed on the first page; later pages reuse the header.
    active_query = None if cursor or lane == LANE_ACTIVE else build_query(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, role=ctx.role, lane=LANE_ACTIVE, hidden_flags=hidden_flags,
    )
    try:
        page, total, other_total = await asyncio.gather(
            list_entries(query, cursor=cursor, limit=limit),
            count_entries(query) if not cursor else _none(),
            count_entries(active_query) if active_query is not None else _none(),
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid cursor.")
    active_total = None if cursor else (total if lane == LANE_ACTIVE else other_total)
    return {
        "items": [present(d, user_id=ctx.user_id, read_before=cursor_read) for d in page["docs"]],
        "next_cursor": page["next_cursor"],
        "total": total,
        "active_total": active_total,
    }


_COMPLETED_WINDOW = timedelta(hours=24)
_COMPLETED_LIMIT = 5


def _tidy_title(raw: str, limit: int = 80) -> str:
    """A title short enough to read on a phone: a pasted link becomes its site and page, long text is cut at a word."""
    text = " ".join((raw or "").split())
    if text.lower().startswith(("http://", "https://")):
        link, _, rest = text.partition(" ")
        parsed = urlparse(link)
        slug = (parsed.path.rstrip("/").rsplit("/", 1)[-1] or "").replace("-", " ").replace("_", " ").strip()
        text = f"{parsed.netloc.removeprefix('www.')}" + (f": {slug}" if slug else "") + (f" {rest}" if rest else "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.:;-") + "…"


def _completed_card(doc: dict) -> dict:
    """Row → the Control Tower's "Recently Completed" card."""
    metadata = doc.get("metadata") or {}
    if doc.get("category") == "content_generated":
        pieces = metadata.get("branchesCount") or 0
        return {
            "id": doc["_id"],
            "kind": "generated",
            "title": _tidy_title(doc.get("subject") or doc.get("title", "")),
            "project": ", ".join(doc.get("platforms") or []),
            # A finished background run does not count its posts here, so it says it finished rather than "0".
            "subtitle": f"{pieces} {'post' if pieces == 1 else 'posts'} made" if pieces else "Finished",
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
@limiter.limit("300/minute")
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
    # The four parts do not depend on each other, so they are read at the same time: this is polled every few seconds, and each
    # read is a round trip to the database.
    completed, live, failed, upcoming = await asyncio.gather(
        activity_entries.find(query).sort([("occurred_at", -1), ("_id", -1)]).limit(_COMPLETED_LIMIT).to_list(length=_COMPLETED_LIMIT),
        _live_work(ctx.workspace_id),
        _recent_failures(ctx.workspace_id),
        _upcoming(ctx.workspace_id),
    )
    return {
        "live": live,
        "failed": failed,
        "upcoming": upcoming,
        "completed": [_completed_card(d) for d in completed],
    }


async def _recent_failures(workspace_id: str) -> list[dict]:
    """Background work that failed in the last day, with the reason it gave, so a failure is never just a missing item.
    A run the member cancelled is their choice, not a failure."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    from app.db.mongo import pipeline_runs as runs_collection

    rows = await runs_collection.find(
        {"workspace_id": workspace_id, "status": "failed", "finished_at": {"$gte": since}}, {"_id": 0},
    ).sort("finished_at", -1).limit(30).to_list(length=30)
    # The same piece of work that failed more than once is one line, and one that was started again and finished is not a failure any more.
    done_after: dict[tuple[str, str], object] = {}
    for r in await runs_collection.find(
        {"workspace_id": workspace_id, "status": "done", "finished_at": {"$gte": since}}, {"kind": 1, "title": 1, "finished_at": 1, "_id": 0},
    ).to_list(length=200):
        key = (r["kind"], _tidy_title(r.get("title", "")))
        done_after[key] = max(done_after.get(key, r["finished_at"]), r["finished_at"])
    seen: set[tuple[str, str, str]] = set()
    kept: list[dict] = []
    for r in rows:
        title = _tidy_title(r.get("title", ""))
        finished = done_after.get((r["kind"], title))
        if finished is not None and finished > r["finished_at"]:
            continue
        signature = (r["kind"], title, r.get("error") or "")
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(r)
        if len(kept) == 5:
            break
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "title": _tidy_title(r.get("title", "")),
            "error": (r.get("error") or "It stopped without saying why. Try starting it again.")[:300],
            "finishedAt": _iso(r.get("finished_at")),
            "href": r.get("href") or _RUN_PAGES.get(r["kind"]),
        }
        for r in kept
    ]


_RUN_PAGES = {
    "text": "/dashboard/pipelines/text",
    "audio": "/dashboard/pipelines/audio",
    "image": "/dashboard/pipelines/image",
    "video": "/dashboard/pipelines/video",
    "odette": "/dashboard/odette",
}
_RUN_LABELS = {"campaign": "Campaign", "text": "Text", "audio": "Audio", "image": "Image", "video": "Video", "odette": "Odette"}


def _run_eta(run: dict) -> str:
    if run["status"] == "paused":
        return "Paused"
    if run["status"] == "queued":
        return "Waiting to start"
    total, done = run.get("steps_total") or 0, run.get("steps_done") or 0
    started = to_utc_datetime(run.get("started_at") or run.get("created_at"))
    if not total or done <= 0 or done >= total or not started:
        return ""
    left = (datetime.now(timezone.utc) - started).total_seconds() / done * (total - done)
    return "<1m" if left < 60 else f"{round(left / 60)}m"


async def _live_work(workspace_id: str) -> list[dict]:
    """Everything being made right now, from one place. Saved background runs are the source for anything that can be
    paused or cancelled (the same record the progress screens read, so both show the same number). Work that only reports
    live progress is added after them, unless a saved run already covers it."""
    saved = [
        r for r in await pipeline_runs.list_runs(workspace_id, include_finished_days=0)
        if r["status"] in pipeline_runs.ACTIVE
    ]
    cards: list[dict] = []
    covered: set[str] = set()
    for run in saved:
        campaign_id = (run.get("ref") or {}).get("campaign_id")
        if campaign_id:
            covered.add(f"campaign:{campaign_id}")
        # A job started from a page that also reports its own live progress carries that live entry's id in its saved
        # payload; the saved run is the one with controls, so the live twin is not listed as a second card.
        live_twin = run.get("live_run_id") or (run.get("payload") or {}).get("live_run_id")
        if live_twin:
            covered.add(str(live_twin))
        public = pipeline_runs.public(run) or {}
        cards.append({
            "id": run["id"],
            "runId": run["id"],
            "kind": run["kind"],
            "title": run.get("title", ""),
            "project": _RUN_LABELS.get(run["kind"], ""),
            "stage": run.get("stage") or "",
            "progress": public.get("progress") or 0,
            "eta": _run_eta(run),
            "startedAt": _iso(run.get("created_at")),
            "status": run["status"],
            "controllable": True,
            "cancelRequested": bool(run.get("cancel_requested")),
            "href": run.get("href") or _RUN_PAGES.get(run["kind"]),
        })
    for live_run in await list_runs(workspace_id):
        if live_run["id"] in covered:
            continue
        kind = live_run.get("kind", "text")
        href = _RUN_PAGES.get(kind)
        if live_run["id"].startswith("campaign:"):
            href = f"/dashboard/pipelines/new?campaignId={live_run['id'].split(':', 1)[1]}"
        cards.append({**live_run, "runId": None, "status": "running", "controllable": False, "href": href})
    cards.sort(key=lambda c: c.get("startedAt") or "")
    return cards


_UPCOMING_WINDOW = timedelta(hours=24)
_UPCOMING_LIMIT = 5


async def _upcoming(workspace_id: str) -> list[dict]:
    """What's queued to happen on its own in the next 24h: scheduled posts
    (publish_scheduled_at is a real datetime now, an ISO string on older rows,
    so both are matched) and automated campaign runs."""
    now = datetime.now(timezone.utc)
    horizon = now + _UPCOMING_WINDOW
    items: list[dict] = []
    due_pieces = await content_pieces.find(
        {
            "workspace_id": workspace_id,
            "publish_status": "queued",
            "deleted": {"$ne": True},
            "$or": [
                {"publish_scheduled_at": {"$lte": horizon}},
                {"publish_scheduled_at": {"$lte": horizon.isoformat()}},
            ],
        },
        {"piece_id": 1, "content": 1, "platform": 1, "publish_target": 1, "publish_scheduled_at": 1},
    ).limit(100).to_list(length=100)
    # Mixed datetime and string rows do not sort together in the database.
    due_pieces.sort(key=lambda p: to_utc_datetime(p.get("publish_scheduled_at")) or horizon)
    for piece in due_pieces[:_UPCOMING_LIMIT]:
        first = ((piece.get("content") or "").strip().splitlines() or [""])[0]
        items.append({
            "id": f"post:{piece['piece_id']}",
            "kind": "scheduled_post",
            "title": first if len(first) <= 60 else first[:57].rstrip() + "…",
            "project": (piece.get("publish_target") or piece.get("platform") or "").capitalize(),
            "subtitle": "Scheduled post",
            "at": iso_utc(piece.get("publish_scheduled_at")),
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
        await inbox_mod.mark_all_read(ctx.workspace_id, ctx.user_id, ctx.role)
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
        await inbox_mod.mark_all_read(ctx.workspace_id, ctx.user_id, ctx.role)
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


@router.get("/allowance")
@limiter.limit("60/minute")
async def allowance(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """What this workspace has left today of what has a daily ceiling (AI pictures), so the member sees it next to what spends it."""
    from app.shared import entitlements

    ent = await entitlements.entitlements_for(ctx.workspace)
    made = int(await entitlements.count_usage(ent, "pictures_day"))
    cap = ent.limit("pictures_day")
    return {"pictures": {"limit": cap, "used": made, "left": None if cap is None else max(0, cap - made)}}


@router.get("/unread-count")
@limiter.limit("120/minute")
async def unread_count(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The sidebar badge — unread items that need attention (the Inbox set)."""
    return {"unread": await inbox_mod.unread_count(ctx.workspace_id, ctx.user_id, ctx.role)}


@router.get("/attention")
@limiter.limit("120/minute")
async def attention(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The one number behind every "needs you" badge (the top bar, the floating pill): unread decisions plus unread failures. `unread` is
    everything unread in the inbox, which also includes finished work and what the assistant did, and is shown more quietly."""
    return {
        "needs_you": await inbox_mod.attention_count(ctx.workspace_id, ctx.user_id, ctx.role),
        "unread": await inbox_mod.unread_count(ctx.workspace_id, ctx.user_id, ctx.role),
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
    elif kind == "video_decision":
        from app.video import automation

        outcome = "accepted" if body.decision == "accept" else "dismissed"
        if body.decision == "snooze":
            await patch_entry(entry_id, {"snoozed_until": until})
        else:
            await automation.apply_decision((doc.get("metadata") or {}).get("recipeId", ""), ctx.workspace_id, body.decision, doc.get("metadata"))
            await patch_entry(entry_id, {
                "lane": LANE_PASSIVE, "status": "success", "decision": {"outcome": outcome}, "decided_at": datetime.now(timezone.utc),
                "metadata": {**(doc.get("metadata") or {}), "decision": outcome.capitalize()},
            })
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


#: How often a live stream looks at its member's access again.
_RECHECK_SECONDS = 60


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
        nonlocal role, cursor_read
        q = live.subscribe(workspace_id)
        last_checked = asyncio.get_event_loop().time()
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected() or live.is_dropped(workspace_id, q):
                    break
                # Membership is looked at again every minute: a person removed from the workspace (or whose role changed) must not keep
                # receiving its activity on a tab left open, and "mark all read" must show up in the rows sent after it.
                if asyncio.get_event_loop().time() - last_checked > _RECHECK_SECONDS:
                    last_checked = asyncio.get_event_loop().time()
                    current = await workspace_members.find_one({"workspace_id": workspace_id, "user_id": user_id})
                    if not current or current.get("status") != "active":
                        yield "event: closed\ndata: {}\n\n"
                        break
                    role = current.get("role", "")
                    cursor_read = await inbox_mod.read_before(workspace_id, user_id)
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
