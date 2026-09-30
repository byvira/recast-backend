"""
Workspace AI processing budget — scaffolding for Odette's Quotas tab. The
budget itself is real and settable; usage now writes for real for call sites
wrapped in app.shared.llm.usage_workspace() — see app/models/ai_usage.py's
docstring.

The Ops LLM health page has its own API now: app/api/v1/ops_llm_health.py (/api/v1/ops/llm).
This file keeps only the budget and usage routes. Owner-gated, same pattern as /api/v1/ops/platforms.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require_ops_admin
from app.db.mongo import workspace_ai_budgets, workspace_ai_usage_daily
from app.models.ai_usage import (
    WorkspaceAIBudget,
    WorkspaceAIBudgetWrite,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_OWNER = require_ops_admin("manage_workspace_settings")


@router.get("/budget")
@limiter.limit("30/minute")
async def get_budget(request: Request, ctx: WorkspaceContext = Depends(_OWNER)) -> WorkspaceAIBudget:
    doc = await workspace_ai_budgets.find_one({"workspace_id": ctx.workspace_id})
    if doc:
        return WorkspaceAIBudget(**doc)
    return WorkspaceAIBudget(id=ctx.workspace_id, workspace_id=ctx.workspace_id)


@router.put("/budget")
@limiter.limit("20/minute")
async def set_budget(
    request: Request, body: WorkspaceAIBudgetWrite, ctx: WorkspaceContext = Depends(_OWNER)
) -> WorkspaceAIBudget:
    now = datetime.now(timezone.utc)
    await workspace_ai_budgets.update_one(
        {"workspace_id": ctx.workspace_id},
        {
            "$set": {"monthly_token_budget": body.monthly_token_budget, "updated_at": now},
            "$setOnInsert": {"id": ctx.workspace_id, "workspace_id": ctx.workspace_id},
        },
        upsert=True,
    )
    doc = await workspace_ai_budgets.find_one({"workspace_id": ctx.workspace_id})
    return WorkspaceAIBudget(**doc)


@router.get("/usage")
@limiter.limit("30/minute")
async def get_usage(request: Request, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    """Real query against a real (currently always-empty) collection — see
    module docstring. Returns zeros honestly rather than a fabricated number
    until LLM call sites are instrumented to write here."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = await workspace_ai_usage_daily.find(
        {"workspace_id": ctx.workspace_id, "date": {"$gte": since}}
    ).to_list(31)
    total_tokens = sum(r.get("tokens_used", 0) for r in rows)
    total_calls = sum(r.get("calls", 0) for r in rows)
    return {
        "window_days": 30,
        "total_tokens": total_tokens,
        "total_calls": total_calls,
        "daily": rows,
        # Every LLM call made inside one of these entry points is attributed
        # to the workspace (via set_usage_workspace/usage_workspace), so this
        # total is real for all of them. Not covered: campaign topic
        # suggestions (no workspace_id reaches that call). Image generation
        # and TTS don't spend LLM tokens, so they never appear here.
        "metered": True,
        "metered_coverage": [
            "text.generation (all platforms, repurpose, batch, campaigns)",
            "audio.localization (translation)",
            "brand.voice_playground", "brand.voice_suggestions", "brand.trait_extraction",
            "analytics", "personal.assistant", "supervisor.reason", "supervisor.synthesize",
        ],
        "enforced": True,
    }
