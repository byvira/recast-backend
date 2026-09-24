"""
Workspace AI processing budget — scaffolding for Odette's Quotas tab. The
budget itself is real and settable; usage now writes for real for call sites
wrapped in app.shared.llm.usage_workspace() — see app/models/ai_usage.py's
docstring.

Also serves the Ops LLM Health page: /health (process-lifetime usage,
latency, cache-hit and error metrics across every Groq/Gemini call — see
app.shared.llm's usage/latency/error tracking, provider- and model-agnostic
so a future audio/video/image model shows up here automatically as soon as
it calls through app.shared.llm) and /notes (a real, manually-logged
issue/security audit trail — never fabricated data). Owner-gated, same
pattern as /api/v1/ops/platforms.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require_ops_admin
from app.db.mongo import ops_llm_notes, workspace_ai_budgets, workspace_ai_usage_daily
from app.models.ai_usage import (
    OpsLLMNote,
    OpsLLMNoteStatusWrite,
    OpsLLMNoteWrite,
    WorkspaceAIBudget,
    WorkspaceAIBudgetWrite,
)
from app.shared.llm import (
    get_circuit_breaker_status,
    get_latency_stats,
    get_recent_errors,
    get_usage_stats,
    llm_health_check,
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
        # Real for Odette (reasoning + synthesis) and Remy (align_draft) —
        # the two agents wrapped in usage_workspace() so far. Other
        # pipelines (text generation, repurpose, scoring, ...) aren't
        # wrapped yet, so a workspace using only those still sees zeros
        # here honestly, not a fabricated total.
        "metered": True,
        "metered_coverage": ["supervisor.reason", "supervisor.synthesize", "personal.align_draft"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ops LLM Health — cross-tenant, platform-staff-only (see
# app.core.auth.require_platform_staff's docstring for why this can't reuse
# _OWNER: it aggregates every workspace's Groq/Gemini activity on this
# server, not just the caller's own).
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health")
@limiter.limit("30/minute")
async def get_llm_health(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    """Process-lifetime usage, cache-hit rate, latency and recent errors,
    across every provider and model this server has called through
    app.shared.llm — Groq and Gemini today, and any audio/video/image model
    added later automatically, as long as it calls through that module's
    call_llm-family functions rather than a bespoke client.

    Not a durable history — resets on server restart. For a persisted,
    per-workspace token/cost trend, see GET /usage instead (which reads a
    real Mongo collection, not process memory).
    """
    usage = get_usage_stats()
    latency = get_latency_stats()

    models: dict[str, dict] = {}
    for key, value in usage.items():
        provider_model, _, metric = key.rpartition(".")
        if not provider_model:
            continue
        row = models.setdefault(
            provider_model,
            {"provider_model": provider_model, "calls": 0, "prompt_tokens": 0,
             "completion_tokens": 0, "cached_tokens": 0},
        )
        row[metric] = value

    rows = []
    for provider_model, row in sorted(models.items()):
        row["cache_hit_rate"] = (
            round(row["cached_tokens"] / row["prompt_tokens"], 3) if row["prompt_tokens"] else 0.0
        )
        lat = latency.get(provider_model)
        row["avg_latency_ms"] = lat["avg_ms"] if lat else None
        row["p95_latency_ms"] = lat["p95_ms"] if lat else None
        rows.append(row)

    return {
        "models": rows,
        "circuit_breaker": get_circuit_breaker_status(),
        "recent_errors": get_recent_errors(),
        "note": "Process-lifetime counters since last server restart, not a durable history.",
    }


@router.post("/ping")
@limiter.limit("6/minute")
async def ping_providers(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    """Live round-trip check against Groq and Gemini — costs a real API call
    to each, so rate-limited separately from /health and never auto-polled
    from the frontend (button-triggered only)."""
    return await llm_health_check()


# ── Notes — manually-logged issue/security audit trail ──────────────────────

@router.get("/notes")
@limiter.limit("30/minute")
async def list_notes(
    request: Request, status: str | None = None, user: dict = Depends(require_platform_staff)
) -> dict:
    q: dict = {}
    if status:
        q["status"] = status
    rows = await ops_llm_notes.find(q).sort("created_at", -1).limit(200).to_list(200)
    for r in rows:
        r["id"] = r.pop("_id")
    return {"items": rows, "total": len(rows)}


@router.post("/notes")
@limiter.limit("20/minute")
async def create_note(
    request: Request, body: OpsLLMNoteWrite, user: dict = Depends(require_platform_staff)
) -> OpsLLMNote:
    note = OpsLLMNote(
        id=str(uuid4()),
        kind=body.kind,
        severity=body.severity,
        title=body.title,
        detail=body.detail,
        created_by=user.get("id", ""),
        created_at=datetime.now(timezone.utc),
    )
    doc = note.model_dump()
    doc["_id"] = doc.pop("id")
    await ops_llm_notes.insert_one(doc)
    return note


@router.patch("/notes/{note_id}")
@limiter.limit("20/minute")
async def update_note_status(
    request: Request, note_id: str, body: OpsLLMNoteStatusWrite, user: dict = Depends(require_platform_staff)
) -> dict:
    update: dict = {"status": body.status.value}
    update["resolved_at"] = datetime.now(timezone.utc) if body.status.value == "resolved" else None
    res = await ops_llm_notes.update_one({"_id": note_id}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Note not found.")
    return {"id": note_id, "status": body.status.value}
