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

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.db.mongo import get_db
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
            {"piece_id": {"$in": piece_ids}},
            {"piece_id": 1, "content": 1, "publish_status": 1, "updated_at": 1},
        ).to_list(length=len(piece_ids))
        piece_by_id = {p["piece_id"]: p for p in pieces}
    for m in metrics:
        piece = piece_by_id.get(m.get("post_id"), {})
        m["content"] = piece.get("content", "")
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
    account_metrics = [AccountMetrics(**d) for d in account_docs]
    post_metrics    = [PostMetrics(**d)    for d in post_docs]
    summary = summarize(account_metrics, post_metrics)

    # Real week-over-week delta for the Home page's Insights Strip — None
    # (not a fabricated 0%) until this workspace has a snapshot at least
    # ~7 days old (see app.pipelines.analytics.snapshots).
    previous_totals = await get_previous_totals(ctx.workspace_id)
    summary["previous_totals"] = previous_totals
    summary["deltas"] = compute_deltas(summary["totals"], previous_totals)
    return summary


@router.get("/refresh")
@limiter.limit("5/minute")
async def trigger_refresh(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    db  = get_db()
    ws  = ctx.workspace_id
    since = datetime.now(timezone.utc) - timedelta(days=7)
    until = datetime.now(timezone.utc)
    account_metrics = await fetch_account_metrics_all(
        workspace_id=ws,
        since=since,
        until=until,
    )
    for m in account_metrics:
        await db["account_metrics"].update_one(
            {"workspace_id": ws, "platform": m.platform},
            {"$set": {
                **m.model_dump(),
                "workspace_id": ws,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    return {
        "refreshed":    True,
        "platforms":    [m.platform for m in account_metrics],
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


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

@router.get("/calendar")
@limiter.limit("30/minute")
async def get_calendar(
    request:       Request,
    year:          int  = Query(default=None),
    month:         int  = Query(default=None),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """
    Return all content pieces for a given month organized by date.
    Used by the calendar view on the Performance page.
    """
    from datetime import datetime, timezone
    import calendar

    now   = datetime.now(timezone.utc)
    year  = year  or now.year
    month = month or now.month

    _, last_day = calendar.monthrange(year, month)
    start = datetime(year, month, 1,        tzinfo=timezone.utc)
    end   = datetime(year, month, last_day, hour=23, minute=59, second=59, tzinfo=timezone.utc)

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
                {"publish_status": "published", "updated_at": {"$gte": start, "$lte": end}},
                {"publish_scheduled_at": {"$gte": start, "$lte": end}},
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
            display_date = piece.get("updated_at") or piece["created_at"]
        elif piece.get("publish_scheduled_at"):
            display_date = piece["publish_scheduled_at"]
        else:
            display_date = piece["created_at"]

        if isinstance(display_date, datetime):
            date_key = display_date.strftime("%Y-%m-%d")
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
            "published_at": (piece.get("updated_at") if publish_status == "published" else None),
            "post_url":     piece.get("platform_post_url"),
        }

        campaign_id = piece.get("campaign_id")

        days[date_key].append({
            "id":               piece.get("piece_id", ""),
            "content_preview":  piece.get("content", "")[:120],
            "status":           publish_status,
            # Calendar used to be read-only — "status" alone (bare
            # publish_status) can't tell drafting apart from staging, both
            # of which read as "pending". Real KanbanStage lets the
            # frontend offer the correct next action (and only that one)
            # per card instead of guessing.
            "stage":            compute_kanban_stage(piece),
            "platforms":        [piece.get("platform", "")] if piece.get("platform") else [],
            "scheduled_at":     piece.get("publish_scheduled_at"),
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
