"""
Analytics API endpoints — workspace-scoped.
GET  /api/v1/analytics/accounts  — account-level metrics across all platforms
GET  /api/v1/analytics/posts     — post-level metrics for published content
GET  /api/v1/analytics/summary   — unified cross-platform summary
GET  /api/v1/analytics/refresh   — manually trigger metrics refresh
POST /api/v1/analytics/ask       — natural language analytics query (agent)
GET  /api/v1/analytics/ask       — full dashboard report (agent, no question needed)

All routes require workspace membership.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db
from app.pipelines.publish.spine import iso_utc
from app.pipelines.text.storage import compute_kanban_stage
from app.pipelines.analytics.aggregator import (
    fetch_account_metrics_all,
    fetch_post_metrics_all,
    summarize,
)
from app.pipelines.analytics.snapshots import compute_deltas, get_previous_totals
from app.agents.analytics.graph import run_analytics
from app.agents.analytics.state import DEFAULT_OVERVIEW_QUESTION

router = APIRouter()
logger = logging.getLogger(__name__)


class AnalyticsAskRequest(BaseModel):
    question: Optional[str] = DEFAULT_OVERVIEW_QUESTION


@router.get("/accounts")
@limiter.limit("20/minute")
async def get_account_metrics(
    request: Request,
    platforms: str = Query(None, description="Comma-separated e.g. instagram,threads"),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    platform_list = platforms.split(",") if platforms else None
    since = datetime.now(timezone.utc) - timedelta(days=7)
    until = datetime.now(timezone.utc)
    metrics = await fetch_account_metrics_all(
        workspace_id=ctx.workspace_id,
        platforms=platform_list,
        since=since,
        until=until,
    )
    return {
        "metrics": [m.model_dump() for m in metrics],
        "total":   len(metrics),
        "period":  {"since": since.isoformat(), "until": until.isoformat()},
    }


@router.get("/posts")
@limiter.limit("20/minute")
async def get_post_metrics(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    db = get_db()
    metrics = await db["post_metrics"].find(
        {"workspace_id": ctx.workspace_id},
        sort=[("fetched_at", -1)],
    ).to_list(length=limit)
    for m in metrics:
        m["id"] = str(m.pop("_id"))

    # post_metrics (app.pipelines.analytics.base.PostMetrics) has no
    # content or published_at field at all — post_id is really the
    # originating piece_id (every analytics fetcher sets post_id=piece_id)
    # — join against content_pieces here so the Performance page's "Top
    # Posts" table has real text and a real date to show instead of an
    # always-empty '""' and a publish date that silently never rendered.
    # published_at mirrors get_calendar's own convention (app.api.v1.
    # analytics.get_calendar) — updated_at only means "published" once
    # publish_status actually says so.
    piece_ids = [m["post_id"] for m in metrics if m.get("post_id")]
    piece_by_id: dict[str, dict] = {}
    if piece_ids:
        pieces = await db["content_pieces"].find(
            # Scoped to this workspace: a post id must never pull in another workspace's text.
            {"piece_id": {"$in": piece_ids}, "workspace_id": ctx.workspace_id},
            {"piece_id": 1, "content": 1, "publish_status": 1, "updated_at": 1, "platform_state": 1, "platform_post_url": 1},
        ).to_list(length=len(piece_ids))
        piece_by_id = {p["piece_id"]: p for p in pieces}
    for m in metrics:
        piece = piece_by_id.get(m.get("post_id"), {})
        m["content"] = piece.get("content", "")
        m["piece_id"] = m.get("post_id")
        m["platform_state"] = piece.get("platform_state")
        # A post that is gone from its platform has no link to offer.
        m["platform_post_url"] = None if piece.get("platform_state") == "removed" else piece.get("platform_post_url")
        m["published_at"] = (
            piece["updated_at"] if piece.get("publish_status") == "published" and piece.get("updated_at") else None
        )

    return {"metrics": metrics, "total": len(metrics)}


@router.get("/summary")
@limiter.limit("20/minute")
async def get_summary(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    db = get_db()
    account_docs = await db["account_metrics"].find(
        {"workspace_id": ctx.workspace_id}
    ).to_list(length=20)
    post_docs = await db["post_metrics"].find(
        {"workspace_id": ctx.workspace_id},
        sort=[("fetched_at", -1)],
    ).to_list(length=100)
    from app.pipelines.analytics.base import AccountMetrics, PostMetrics
    # Posts that were removed on their platform are left out of the totals: their last numbers are history, not current reach.
    removed = {
        p["piece_id"] async for p in db["content_pieces"].find(
            {"workspace_id": ctx.workspace_id, "platform_state": "removed"}, {"piece_id": 1},
        )
    }
    account_metrics = [AccountMetrics(**d) for d in account_docs]
    post_metrics    = [PostMetrics(**d)    for d in post_docs if d.get("post_id") not in removed]
    summary = summarize(account_metrics, post_metrics)

    # Real week-over-week delta for the Home page's Insights Strip — None
    # (not a fabricated 0%) until this workspace has a snapshot at least
    # ~7 days old (see app.pipelines.analytics.snapshots).
    previous_totals = await get_previous_totals(ctx.workspace_id)
    summary["previous_totals"] = previous_totals
    summary["deltas"] = compute_deltas(summary["totals"], previous_totals)
    return summary


async def _window_counts(coll, base: dict, since_week: datetime, since_prior: datetime, field: str = "created_at") -> dict:
    """How many documents exist in total, in the last 7 days, and in the 7 days before that, plus the newest date."""
    total = await coll.count_documents(base)
    this_week = await coll.count_documents({**base, field: {"$gte": since_week}})
    prior_week = await coll.count_documents({**base, field: {"$gte": since_prior, "$lt": since_week}})
    newest = await coll.find(base, {field: 1, "_id": 0}).sort(field, -1).limit(1).to_list(length=1)
    last = newest[0].get(field) if newest else None
    return {"total": total, "this_week": this_week, "prior_week": prior_week, "last_created_at": iso_utc(last) if last else None}


@router.get("/pipeline-summary")
@limiter.limit("30/minute")
async def pipeline_summary(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """What each pipeline has made: totals, the last 7 days against the 7 before, the newest item, and how many wait for
    approval. Counts only, so Home does not have to load every item."""
    db = get_db()
    ws = ctx.workspace_id
    now = datetime.now(timezone.utc)
    week, prior = now - timedelta(days=7), now - timedelta(days=14)

    text = await _window_counts(
        db["content_pieces"], {"workspace_id": ws, "deleted": {"$ne": True}, "archived": {"$ne": True}}, week, prior,
    )
    text["pending_approval"] = await db["content_pieces"].count_documents(
        {"workspace_id": ws, "deleted": {"$ne": True}, "archived": {"$ne": True}, "approval_status": "pending"},
    )

    out = {"text": text}
    for kind, coll in (("audio", "audio_assets"), ("image", "image_assets")):
        base = {"workspace_id": ws, "replaced_by": {"$exists": False}} if kind == "image" else {"workspace_id": ws}
        counts = await _window_counts(db[coll], base, week, prior)
        counts["pending_approval"] = await db[coll].count_documents({**base, "approval_status": "pending"})
        out[kind] = counts

    clips_base = [{"$match": {"workspace_id": ws, "video_clips.0": {"$exists": True}}}, {"$unwind": "$video_clips"}]
    video = {"total": 0, "this_week": 0, "prior_week": 0, "last_created_at": None, "pending_approval": 0}
    async for row in db["audio_assets"].aggregate(clips_base + [{"$group": {
        "_id": None,
        "total": {"$sum": 1},
        "this_week": {"$sum": {"$cond": [{"$gte": ["$video_clips.created_at", week]}, 1, 0]}},
        "prior_week": {"$sum": {"$cond": [{"$and": [{"$gte": ["$video_clips.created_at", prior]}, {"$lt": ["$video_clips.created_at", week]}]}, 1, 0]}},
        "last": {"$max": "$video_clips.created_at"},
    }}]):
        video.update(total=row["total"], this_week=row["this_week"], prior_week=row["prior_week"],
                     last_created_at=iso_utc(row["last"]) if row.get("last") else None)
    out["video"] = video

    # With no review step, nothing is waiting for review: the count would only confuse a person who has nobody to ask.
    from app.shared.tier_policy import review_required

    if not await review_required(ws):
        for entry in out.values():
            entry["pending_approval"] = 0

    # How the saved background runs of the last 30 days went, per kind: how long a finished one took and how many of the
    # finished ones worked. Cancelled runs are the member's choice, so they count in neither.
    stats = await _run_stats(ws, now - timedelta(days=30))
    for kind, entry in out.items():
        entry.update(stats.get(kind, {"runs_30d": 0, "avg_seconds": None, "success_rate": None}))
    # What is being made right now, so a pipeline in use never reads as idle. A campaign writes posts, so its runs count
    # under text; video renders are kept in their own record and count while they are still running.
    for entry in out.values():
        entry["in_progress"] = 0
    async for row in db["pipeline_runs"].aggregate([
        {"$match": {"workspace_id": ws, "status": {"$in": ["queued", "running", "paused"]}}},
        {"$group": {"_id": "$kind", "n": {"$sum": 1}}},
    ]):
        kind = "text" if row["_id"] == "campaign" else row["_id"]
        if kind in out:
            out[kind]["in_progress"] += row["n"]
    out["video"]["in_progress"] += await db["audio_render_jobs"].count_documents(
        {"workspace_id": ws, "kind": "video", "status": "running", "started_at": {"$gte": now - timedelta(minutes=30)}},
    )
    if out["text"]["avg_seconds"] is None:
        from app.shared.activity.runs import median_run_seconds

        median = await median_run_seconds(ws)
        out["text"]["avg_seconds"] = round(median, 1) if median else None
    return out


async def _run_stats(workspace_id: str, since: datetime) -> dict[str, dict]:
    rows = get_db()["pipeline_runs"].aggregate([
        {"$match": {"workspace_id": workspace_id, "finished_at": {"$gte": since}, "status": {"$in": ["done", "failed"]}}},
        {"$group": {
            "_id": "$kind",
            "done": {"$sum": {"$cond": [{"$eq": ["$status", "done"]}, 1, 0]}},
            "failed": {"$sum": {"$cond": [{"$eq": ["$status", "failed"]}, 1, 0]}},
            "avg_ms": {"$avg": {"$cond": [
                {"$and": [{"$eq": ["$status", "done"]}, {"$ne": ["$started_at", None]}]},
                {"$subtract": ["$finished_at", "$started_at"]}, None,
            ]}},
        }},
    ])
    out: dict[str, dict] = {}
    async for row in rows:
        total = row["done"] + row["failed"]
        out[row["_id"]] = {
            "runs_30d": total,
            "avg_seconds": round(row["avg_ms"] / 1000, 1) if row.get("avg_ms") is not None else None,
            "success_rate": round(100 * row["done"] / total, 1) if total else None,
        }
    return out


async def _refresh_now(ctx: WorkspaceContext) -> dict:
    """Refresh this workspace's account numbers AND the numbers of its published posts, the same work the scheduled job
    does. The button used to refresh accounts only, so post numbers never moved when someone asked for fresh data."""
    from app.pipelines.analytics.scheduler import _refresh_workspace_analytics

    db = get_db()
    ws = ctx.workspace_id
    await _refresh_workspace_analytics(db, ws)
    platforms = [d["platform"] async for d in db["account_metrics"].find({"workspace_id": ws}, {"platform": 1})]
    return {
        "refreshed":    True,
        "platforms":    platforms,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/refresh")
@limiter.limit("5/minute")
async def trigger_refresh(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    return await _refresh_now(ctx)


@router.post("/refresh")
@limiter.limit("5/minute")
async def trigger_refresh_post(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The same refresh as GET /refresh, under the right verb: it changes stored data, so it is a POST. GET stays for the
    screens that already call it."""
    return await _refresh_now(ctx)


_COUNT_FIELDS = ("likes", "comments", "shares", "reposts", "saves", "clicks", "impressions", "reach", "views")


class SelfReportedMetrics(BaseModel):
    """Numbers a member read off the platform themselves, for places Recast cannot read results from."""

    views: int = Field(0, ge=0, le=1_000_000_000)
    clicks: int = Field(0, ge=0, le=1_000_000_000)
    likes: int = Field(0, ge=0, le=1_000_000_000)
    comments: int = Field(0, ge=0, le=1_000_000_000)
    shares: int = Field(0, ge=0, le=1_000_000_000)


@router.put("/posts/{piece_id}/self-reported")
@limiter.limit("60/minute")
async def save_self_reported_metrics(
    request: Request,
    piece_id: str,
    body: SelfReportedMetrics,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Saves results typed in by hand for a published post (X, a blog, a newsletter, anywhere with no connected results). They are
    marked self-reported so nobody mistakes them for numbers Recast read, and they replace the previous typed-in figures."""
    from app.db.mongo import content_pieces

    piece = await content_pieces.find_one(
        {"piece_id": piece_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}, {"platform": 1, "publish_status": 1},
    )
    from app.pipelines.publish.spine import platform_key

    if not piece:
        raise HTTPException(status_code=404, detail="That post wasn't found.")
    if piece.get("publish_status") != "published":
        raise HTTPException(status_code=400, detail="Results can be entered once the post is published.")

    now = datetime.now(timezone.utc)
    values = body.model_dump()
    values["impressions"] = values["views"]
    await get_db()["post_metrics"].update_one(
        {"workspace_id": ctx.workspace_id, "post_id": piece_id, "source": "self_reported"},
        {"$set": {
            **values, "workspace_id": ctx.workspace_id, "post_id": piece_id, "platform": platform_key(piece.get("platform")), "platform_post_id": piece_id,
            "source": "self_reported", "entered_by": ctx.user_id, "fetched_at": now, "fetch_ok": True,
        }},
        upsert=True,
    )
    return {"piece_id": piece_id, "source": "self_reported", "metrics": values, "saved_at": iso_utc(now)}


@router.get("/assets/{asset_type}/{asset_id}")
@limiter.limit("60/minute")
async def asset_performance(
    request: Request,
    asset_type: str,
    asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """How the posts a picture or a recording went out in have done, added up. The post is what gets published and measured;
    an asset knows which posts it is attached to, so its results are those posts' results. Posts that are not published yet,
    or whose numbers have not been read yet, are listed without numbers and add nothing to the totals."""
    from fastapi import HTTPException

    from app.db.mongo import audio_assets, content_pieces, image_assets

    collection = {"image": image_assets, "audio": audio_assets}.get(asset_type)
    if collection is None:
        raise HTTPException(status_code=404, detail="Results are available for pictures (image) and recordings (audio).")
    doc = await collection.find_one({"id": asset_id, "workspace_id": ctx.workspace_id}, {"linked_pieces": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="That asset wasn't found.")

    db = get_db()
    posts: list[dict] = []
    totals = {name: 0 for name in _COUNT_FIELDS}
    measured = 0
    for link in (doc.get("linked_pieces") or [])[:100]:
        piece_id = link.get("piece_id")
        piece = await content_pieces.find_one(
            {"piece_id": piece_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
            {"platform": 1, "publish_status": 1, "platform_post_url": 1, "published_at": 1, "content": 1},
        )
        if not piece:
            continue
        metrics = await db["post_metrics"].find_one({"workspace_id": ctx.workspace_id, "post_id": piece_id})
        entry = {
            "piece_id": piece_id,
            "platform": piece.get("platform"),
            "publish_status": piece.get("publish_status"),
            "platform_post_url": piece.get("platform_post_url"),
            "published_at": iso_utc(piece.get("published_at")) if piece.get("published_at") else None,
            "preview": (piece.get("content") or "")[:80],
            "metrics": None,
        }
        if metrics:
            entry["metrics"] = {name: int(metrics.get(name) or 0) for name in _COUNT_FIELDS}
            entry["metrics"]["fetched_at"] = iso_utc(metrics.get("fetched_at")) if metrics.get("fetched_at") else None
            for name in _COUNT_FIELDS:
                totals[name] += entry["metrics"][name]
            measured += 1
        posts.append(entry)
    return {"asset_type": asset_type, "asset_id": asset_id, "post_count": len(posts), "measured_posts": measured, "totals": totals, "posts": posts}


@router.post("/ask")
@limiter.limit("10/minute")
async def ask_analytics(
    request: Request,
    body: AnalyticsAskRequest,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    result = await run_analytics(
        workspace_id=ctx.workspace_id,
        # An empty/whitespace question from the client means "no real
        # question" just as much as omitting the field does — falls back to
        # the same default the graph nodes compare against to decide
        # whether to run the fixed overview structure or genuinely answer
        # a specific question.
        question=(body.question or "").strip() or DEFAULT_OVERVIEW_QUESTION,
        user_id=ctx.user_id,
    )
    return {
        "question":          result["question"],
        "report":            result["report"],
        "analysis":          result["analysis"],
        "recommendations":   result["recommendations"],
        "platforms_checked": result["connected_platforms"],
        "errors":            result["errors"],
    }


@router.get("/ask")
@limiter.limit("10/minute")
async def get_dashboard_report(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    result = await run_analytics(
        workspace_id=ctx.workspace_id,
        question=DEFAULT_OVERVIEW_QUESTION,
        user_id=ctx.user_id,
    )
    return {
        "report":            result["report"],
        "analysis":          result["analysis"],
        "recommendations":   result["recommendations"],
        # Matches POST /ask's key name (previously "platforms" here vs
        # "platforms_checked" there for the exact same value) — the
        # frontend's AnalyticsReport type already only ever declared
        # platforms_checked, so this endpoint's key never actually matched
        # what the type (and now the UI) expects.
        "platforms_checked": result["connected_platforms"],
        "account_metrics":   result["account_metrics"],
        "post_metrics":      result["post_metrics"],
        "errors":            result["errors"],
    }

TOP_PERFORMER_SHARE = 0.05   # the Library tab is labelled "Top Performers (Top 5%)"
TOP_PERFORMER_CHECKPOINT = "24h"


@router.get("/top-performers")
@limiter.limit("30/minute")
async def top_performers(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The workspace's top 5% of published posts by engagement rate,
    measured at the same age for every post — 24h after publishing
    (``post_metric_checkpoints``) — so a week-old post isn't compared with
    one that went out this morning. At least one post once any has been
    measured; ``measured == 0`` means there's nothing to rank yet.

    Text pipeline only today — text posts are the only published pieces.
    When audio/video/image publishing exists, add a ``pipeline`` filter here
    (checkpoints already carry ``pipeline_type``) so each Library tab ranks
    within its own medium. See docs/DEFERRED_AND_PARTIAL_SCOPE.md PAR-013."""
    import math
    from app.db.mongo import post_metric_checkpoints

    rows = await post_metric_checkpoints.find(
        {"workspace_id": ctx.workspace_id, "checkpoint": TOP_PERFORMER_CHECKPOINT},
        {"piece_id": 1, "metrics.engagement_rate": 1, "platform": 1},
    ).to_list(length=5000)
    ranked = sorted(
        rows, key=lambda r: float((r.get("metrics") or {}).get("engagement_rate") or 0.0), reverse=True,
    )
    top_n = math.ceil(len(ranked) * TOP_PERFORMER_SHARE) if ranked else 0
    top = [r for r in ranked[:top_n] if float((r.get("metrics") or {}).get("engagement_rate") or 0.0) > 0]
    return {
        "checkpoint": TOP_PERFORMER_CHECKPOINT,
        "share": TOP_PERFORMER_SHARE,
        "measured": len(ranked),
        "items": [
            {
                "piece_id": r["piece_id"],
                "engagement_rate": float((r.get("metrics") or {}).get("engagement_rate") or 0.0),
                "rank": i + 1,
            }
            for i, r in enumerate(top)
        ],
    }


@router.get("/calendar")
@limiter.limit("30/minute")
async def get_calendar(
    request:       Request,
    year:          int  = Query(default=None),
    month:         int  = Query(default=None),
    # JavaScript's Date.getTimezoneOffset(): minutes the viewer's clock is
    # behind UTC (India is -330). Days are bucketed on the viewer's own clock,
    # so a post scheduled for 11:30pm local shows on that day, not the next.
    tz_offset_minutes: int = Query(default=0, ge=-840, le=840),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """
    Return all content pieces for a given month organized by date.
    Used by the calendar view on the Performance page.
    """
    from datetime import datetime, timedelta, timezone
    import calendar

    now   = datetime.now(timezone.utc)
    year  = year  or now.year
    month = month or now.month

    _, last_day = calendar.monthrange(year, month)
    offset = timedelta(minutes=tz_offset_minutes)
    # The viewer's month runs midnight to midnight on their clock, which is
    # shifted from UTC by their offset.
    start = datetime(year, month, 1,        tzinfo=timezone.utc) + offset
    end   = datetime(year, month, last_day, hour=23, minute=59, second=59, tzinfo=timezone.utc) + offset

    db = get_db()

    # Real publish_status values (app.models.text.PublishStatus):
    # pending / queued / publishing / published / failed. "draft"/"scheduled"
    # never existed in the real data — the query below used to filter on
    # them and so silently excluded every real pending or queued piece.
    pieces = await db["content_pieces"].find(
        {
            "workspace_id": ctx.workspace_id,
            "deleted": {"$ne": True},
            "$or": [
                # A published post sits on the day it went live (published_at),
                # not on the last day it was edited or archived. Older posts
                # without published_at fall back to updated_at.
                {"publish_status": "published", "published_at": {"$gte": start, "$lte": end}},
                {
                    "publish_status": "published",
                    "published_at": {"$exists": False},
                    "updated_at": {"$gte": start, "$lte": end},
                },
                {"publish_scheduled_at": {"$gte": start, "$lte": end}},
                # Some paths store the schedule as an ISO string, which a
                # date range never matches; ISO strings sort like dates.
                {"publish_scheduled_at": {"$gte": start.isoformat(), "$lte": end.isoformat()}},
                # A piece generated with a planned time sits on that day
                # until it is approved and queued.
                {"intended_publish_at": {"$gte": start, "$lte": end}},
                {
                    "publish_status": {"$in": ["pending", "failed"]},
                    "created_at":     {"$gte": start, "$lte": end},
                },
            ],
        },
        sort=[("created_at", -1)],
    ).to_list(length=500)

    # One lookup for every campaign referenced this month, not one per piece.
    campaign_ids = {piece["campaign_id"] for piece in pieces if piece.get("campaign_id")}
    campaign_names: dict[str, str] = {}
    if campaign_ids:
        from app.db.mongo import get_campaigns_collection
        campaign_docs = await get_campaigns_collection().find(
            {"id": {"$in": list(campaign_ids)}}, {"id": 1, "name": 1}
        ).to_list(length=len(campaign_ids))
        campaign_names = {c["id"]: c["name"] for c in campaign_docs}

    days: dict[str, list] = {}
    summary = {"total": 0, "published": 0, "queued": 0, "pending": 0, "failed": 0}

    for piece in pieces:
        publish_status = piece.get("publish_status", "pending")

        if publish_status == "published":
            display_date = piece.get("published_at") or piece.get("updated_at") or piece["created_at"]
        elif piece.get("publish_scheduled_at"):
            display_date = piece["publish_scheduled_at"]
        elif piece.get("intended_publish_at"):
            display_date = piece["intended_publish_at"]
        else:
            display_date = piece["created_at"]

        if isinstance(display_date, str):
            try:
                display_date = datetime.fromisoformat(display_date.replace("Z", "+00:00"))
            except ValueError:
                pass
        if isinstance(display_date, datetime):
            date_key = (display_date - offset).strftime("%Y-%m-%d")
        else:
            date_key = str(display_date)[:10]

        if date_key not in days:
            days[date_key] = []

        # Each content_pieces document is already exactly one platform's
        # content (piece["platform"]) — there is no real multi-platform
        # bundle per piece, so "platform_results" (an array field nothing
        # ever wrote) is synthesized here as that one real result, not read
        # from a field that was always empty.
        platform_result = {
            "platform":     piece.get("platform", ""),
            "status":       publish_status,
            "published_at": (
                (piece.get("published_at") or piece.get("updated_at")) if publish_status == "published" else None
            ),
            "post_url":     piece.get("platform_post_url"),
            # Row 9 — same real publish outcome the Activity Log and the
            # piece's own media_dropped_reason field already carry.
            "media_dropped_reason": piece.get("media_dropped_reason"),
        }

        campaign_id = piece.get("campaign_id")

        days[date_key].append({
            "id":               piece.get("piece_id", ""),
            "content_preview":  piece.get("content", "")[:120],
            # Row 7/9 — whether this piece has a default (or attached) visual,
            # for the calendar's compact per-day cards (too small for a full
            # thumbnail) to show a real media indicator, not a guess.
            "has_media":        bool(piece.get("media")),
            "status":           publish_status,
            # Calendar used to be read-only — "status" alone (bare
            # publish_status) can't tell drafting apart from staging, both
            # of which read as "pending". Real KanbanStage lets the
            # frontend offer the correct next action (and only that one)
            # per card instead of guessing.
            "stage":            compute_kanban_stage(piece),
            "platforms":        [piece.get("platform", "")] if piece.get("platform") else [],
            "scheduled_at":     iso_utc(piece.get("publish_scheduled_at")),
            # True while the post's platform is paused, retired or its account was disconnected by Ops: the post is
            # kept, not cancelled, and will not go out until the hold is released.
            "held":             bool(piece.get("hold")),
            "created_at":       piece.get("created_at"),
            "platform_results": [platform_result],
            "campaign_id":      campaign_id,
            "campaign_name":    campaign_names.get(campaign_id) if campaign_id else None,
        })

        summary["total"] += 1
        # "publishing" is the few-seconds in-flight state — folded into
        # "queued" for this monthly summary rather than adding a bucket
        # a user would almost never actually see non-zero.
        bucket = "queued" if publish_status == "publishing" else publish_status
        if bucket in summary:
            summary[bucket] += 1

    return {
        "year":    year,
        "month":   month,
        "days":    days,
        "summary": summary,
    }
