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
from app.db.mongo import (
    audio_assets,
    content_pieces,
    image_assets,
    workspace_ai_budgets,
    workspace_ai_usage_daily,
    workspace_insights,
)
from app.shared import ai_credits
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


@router.get("/credits")
@limiter.limit("30/minute")
async def get_credits(request: Request, ctx: WorkspaceContext = Depends(_OWNER)) -> dict:
    """Everything the AI Credits panel shows, from what this workspace really did in the last 30 days (the same window the
    cap is enforced over): tokens against the cap, how many things were made in each area, and one sentence about how AI is
    being used. Nothing is a fixed sample; with no activity every count is zero."""
    now = datetime.now(timezone.utc)
    since_dt = now - timedelta(days=ai_credits.WINDOW_DAYS)
    since = since_dt.strftime("%Y-%m-%d")
    ws = ctx.workspace_id

    rows = await workspace_ai_usage_daily.find({"workspace_id": ws, "date": {"$gte": since}}, {"_id": 0}).to_list(ai_credits.WINDOW_DAYS + 1)
    budget_doc = await workspace_ai_budgets.find_one({"workspace_id": ws}, {"monthly_token_budget": 1})
    cap = (budget_doc or {}).get("monthly_token_budget")
    facts = ai_credits.window_facts(rows, now.date())

    video_pipeline = [
        {"$match": {"workspace_id": ws, "video_clips.created_at": {"$gte": since_dt}}},
        {"$unwind": "$video_clips"},
        {"$match": {"video_clips.created_at": {"$gte": since_dt}}},
        {"$count": "n"},
    ]
    video_rows = await audio_assets.aggregate(video_pipeline).to_list(1)
    counts = {
        "voice_audio": await audio_assets.count_documents({"workspace_id": ws, "created_at": {"$gte": since_dt}}),
        "video": int(video_rows[0]["n"]) if video_rows else 0,
        "posts": await content_pieces.count_documents({"workspace_id": ws, "created_at": {"$gte": since_dt}, "deleted": {"$ne": True}}),
        "images": await image_assets.count_documents({"workspace_id": ws, "created_at": {"$gte": since_dt}}),
        "insights": await workspace_insights.count_documents({"workspace_id": ws, "created_at": {"$gte": since_dt}}),
    }
    used_percent = min(100, round(100 * facts["tokens"] / cap)) if cap else None
    return {
        "window_days": ai_credits.WINDOW_DAYS,
        "tokens_used": facts["tokens"],
        "calls": facts["calls"],
        "cap": cap,
        "used_percent": used_percent,
        "oldest_drops_off_in_days": facts["oldest_drops_off_in_days"],
        "categories": ai_credits.category_rows(counts),
        "insight": ai_credits.insight(cap=cap, tokens=facts["tokens"], pace=facts["daily_pace"], counts=counts),
        # tokens are only spent by writing and analysis; pictures, voice and video are counted as things made
        "note": "Pictures, voice and video do not use AI tokens, so they are counted as things made, not as tokens.",
    }
