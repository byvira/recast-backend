"""
Analytics API endpoints.
GET  /api/v1/analytics/accounts  — account-level metrics across all platforms
GET  /api/v1/analytics/posts     — post-level metrics for published content
GET  /api/v1/analytics/summary   — unified cross-platform summary
GET  /api/v1/analytics/refresh   — manually trigger metrics refresh
POST /api/v1/analytics/ask       — natural language analytics query (agent)
GET  /api/v1/analytics/ask       — full dashboard report (agent, no question needed)
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.db.mongo import get_db
from app.pipelines.analytics.aggregator import (
    fetch_account_metrics_all,
    fetch_post_metrics_all,
    summarize,
)
from app.agents.analytics.graph import analytics_graph
from app.agents.analytics.state import build_initial_state

router = APIRouter()
logger = logging.getLogger(__name__)


class AnalyticsAskRequest(BaseModel):
    question: Optional[str] = "Give me a full performance overview for the last 7 days."


@router.get("/accounts")
@limiter.limit("20/minute")
async def get_account_metrics(
    request: Request,
    platforms: str = Query(None, description="Comma-separated e.g. instagram,threads"),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    platform_list = platforms.split(",") if platforms else None
    since = datetime.now(timezone.utc) - timedelta(days=7)
    until = datetime.now(timezone.utc)
    metrics = await fetch_account_metrics_all(
        user_id=current_user["id"],
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    db = get_db()
    metrics = await db["post_metrics"].find(
        {"user_id": current_user["id"]},
        sort=[("fetched_at", -1)],
    ).to_list(length=limit)
    for m in metrics:
        m["id"] = str(m.pop("_id"))
    return {"metrics": metrics, "total": len(metrics)}


@router.get("/summary")
@limiter.limit("20/minute")
async def get_summary(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    db      = get_db()
    user_id = current_user["id"]
    account_docs = await db["account_metrics"].find(
        {"user_id": user_id}
    ).to_list(length=20)
    post_docs = await db["post_metrics"].find(
        {"user_id": user_id},
        sort=[("fetched_at", -1)],
    ).to_list(length=100)
    from app.pipelines.analytics.base import AccountMetrics, PostMetrics
    account_metrics = [AccountMetrics(**d) for d in account_docs]
    post_metrics    = [PostMetrics(**d)    for d in post_docs]
    return summarize(account_metrics, post_metrics)


@router.get("/refresh")
@limiter.limit("5/minute")
async def trigger_refresh(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    db      = get_db()
    user_id = current_user["id"]
    since   = datetime.now(timezone.utc) - timedelta(days=7)
    until   = datetime.now(timezone.utc)
    account_metrics = await fetch_account_metrics_all(
        user_id=user_id,
        since=since,
        until=until,
    )
    for m in account_metrics:
        await db["account_metrics"].update_one(
            {"user_id": user_id, "platform": m.platform},
            {"$set": {
                **m.model_dump(),
                "user_id":    user_id,
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    state  = build_initial_state(
        user_id=current_user["id"],
        question=body.question,
    )
    result = await analytics_graph.ainvoke(state)
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    state  = build_initial_state(
        user_id=current_user["id"],
        question="Give me a full performance overview for the last 7 days.",
    )
    result = await analytics_graph.ainvoke(state)
    return {
        "report":          result["report"],
        "analysis":        result["analysis"],
        "recommendations": result["recommendations"],
        "platforms":       result["connected_platforms"],
        "account_metrics": result["account_metrics"],
        "post_metrics":    result["post_metrics"],
        "errors":          result["errors"],
    }

@router.get("/calendar")
@limiter.limit("30/minute")
async def get_calendar(
    request:       Request,
    year:          int  = Query(default=None),
    month:         int  = Query(default=None),
    current_user:  dict[str, Any] = Depends(get_current_user),
) -> dict:
    """
    Return all content pieces for a given month organized by date.
    Used by the calendar view on the Performance page.

    Query params:
      year:  int (defaults to current year)
      month: int (defaults to current month, 1-12)

    Returns:
      {
        year: 2026,
        month: 6,
        days: {
          "2026-06-08": [
            {
              id, title, content_preview, platforms,
              status, scheduled_at, published_at,
              platform_results
            }
          ],
          "2026-06-10": [...]
        },
        summary: {
          total:      12,
          published:  8,
          scheduled:  2,
          queued:     1,
          draft:      1,
        }
      }
    """
    from datetime import datetime, timezone
    import calendar

    now   = datetime.now(timezone.utc)
    year  = year  or now.year
    month = month or now.month

    # Build date range for the month
    _, last_day = calendar.monthrange(year, month)
    start = datetime(year, month, 1,        tzinfo=timezone.utc)
    end   = datetime(year, month, last_day, hour=23, minute=59, second=59, tzinfo=timezone.utc)

    db = get_db()

    pieces = await db["content_pieces"].find(
        {
            "user_id": current_user["id"],
            "deleted": {"$ne": True},
            "$or": [
                # Published in this month
                {
                    "publish_status": "published",
                    "platform_results": {
                        "$elemMatch": {
                            "published_at": {"$gte": start, "$lte": end}
                        }
                    }
                },
                # Scheduled for this month
                {
                    "publish_scheduled_at": {"$gte": start, "$lte": end}
                },
                # Created this month (drafts/queued)
                {
                    "publish_status": {"$in": ["draft", "queued"]},
                    "created_at":     {"$gte": start, "$lte": end}
                },
            ],
        },
        sort=[("created_at", -1)],
    ).to_list(length=500)

    # Organize by date
    days: dict[str, list] = {}
    summary = {"total": 0, "published": 0, "scheduled": 0, "queued": 0, "draft": 0}

    for piece in pieces:
        if piece.get("publish_status") == "published":
            # Use the first successful published_at from platform_results
            published_dates = [
                r["published_at"]
                for r in piece.get("platform_results", [])
                if r.get("published_at") and r.get("status") == "published"
            ]
            display_date = min(published_dates) if published_dates else piece["created_at"]
        elif piece.get("publish_scheduled_at"):
            display_date = piece["publish_scheduled_at"]
        else:
            display_date = piece["created_at"]

        # Normalize to date string YYYY-MM-DD
        if isinstance(display_date, datetime):
            date_key = display_date.strftime("%Y-%m-%d")
        else:
            date_key = str(display_date)[:10]

        if date_key not in days:
            days[date_key] = []

        # Extract platforms from platform_results
        platforms = list({
            r["platform"]
            for r in piece.get("platform_results", [])
        })

        days[date_key].append({
            "id":               str(piece.get("id", piece["_id"])),
            "content_preview":  piece.get("content", "")[:120],
            "status":           piece.get("publish_status", "draft"),
            "platforms":        platforms,
            "scheduled_at":     piece.get("publish_scheduled_at"),
            "created_at":       piece.get("created_at"),
            "platform_results": [
                {
                    "platform":     r.get("platform"),
                    "status":       r.get("status"),
                    "published_at": r.get("published_at"),
                    "post_url":     r.get("post_url"),
                }
                for r in piece.get("platform_results", [])
            ],
        })

        # Update summary
        status = piece.get("publish_status", "draft")
        summary["total"] += 1
        if status in summary:
            summary[status] += 1

    return {
        "year":    year,
        "month":   month,
        "days":    days,
        "summary": summary,
    }
