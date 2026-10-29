"""The workspace's credits: the month's balance, its history, the costs table, and the owner's lower monthly limit.

Every member may read the balance, history and costs, because everyone spends from the same pool. Only an owner (or an Ops master
admin) sets the monthly limit. The token totals stay at /usage for Ops, since they are what the AI providers bill.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.billing import costs as C
from app.billing import credits
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require_ops_admin
from app.db.mongo import workspace_ai_usage_daily

router = APIRouter()
logger = logging.getLogger(__name__)

_OWNER = require_ops_admin("manage_workspace_settings")


async def _limit_view(workspace_id: str) -> dict[str, Any]:
    s = await credits.summary(workspace_id)
    return {
        "workspace_id": workspace_id, "monthly_credit_limit": s["owner_limit"], "plan_total": s["plan_total"],
        "enforced": s["enforced"], "resets_on": s["resets_on"],
    }


@router.get("/budget")
@limiter.limit("30/minute")
async def get_budget(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """The owner's monthly credit limit (None when not set) and the plan's own number this month."""
    return await _limit_view(ctx.workspace_id)


class CreditLimitWrite(BaseModel):
    monthly_credit_limit: Optional[int] = Field(None, ge=0, le=10_000_000)


@router.put("/budget")
@limiter.limit("20/minute")
async def set_budget(request: Request, body: CreditLimitWrite, ctx: WorkspaceContext = Depends(_OWNER)) -> dict[str, Any]:
    """Set a lower monthly credit limit for the workspace, or clear it with null. Never above what the plan gives."""
    await credits.set_owner_limit(ctx.workspace_id, body.monthly_credit_limit)
    return await _limit_view(ctx.workspace_id)


@router.get("/usage")
@limiter.limit("30/minute")
async def get_usage(request: Request, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    """AI tokens used in the last 30 days, the number the AI providers bill. Credits are what members spend."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = await workspace_ai_usage_daily.find({"workspace_id": ctx.workspace_id, "date": {"$gte": since}}, {"_id": 0}).to_list(31)
    return {
        "window_days": 30,
        "total_tokens": sum(int(r.get("tokens_used", 0) or 0) for r in rows),
        "total_calls": sum(int(r.get("calls", 0) or 0) for r in rows),
        "daily": rows,
        "metered": True,
    }


@router.get("/credits")
@limiter.limit("60/minute")
async def get_credits(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """This month's credits for the workspace's pool: allowance, used, left, the reset date, and where they went."""
    return await credits.summary(ctx.workspace_id)


@router.get("/credits/history")
@limiter.limit("60/minute")
async def get_credit_history(
    request: Request,
    period: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
    before: Optional[str] = Query(None, max_length=40),
    limit: int = Query(50, ge=1, le=200),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """Every charge, grant and refund this month (or the month asked for), newest first."""
    return await credits.ledger(ctx.workspace_id, period=period, limit=limit, before=before)


@router.get("/credit-costs")
@limiter.limit("60/minute")
async def get_credit_costs(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """What each action costs, so screens can say a price before a run."""
    table = await C.costs()
    return {"modules": [{"key": k, "label": v} for k, v in C.MODULES.items()], "costs": [C.public_row(k, v) for k, v in table.items()]}
