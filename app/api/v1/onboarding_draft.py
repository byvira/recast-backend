"""Onboarding draft routes — save progress and resume from any device.

Endpoints
---------
POST   /api/v1/onboarding/draft         Upsert draft on every state change
GET    /api/v1/onboarding/draft         Resume — returns 404 when no active draft (not an error)
DELETE /api/v1/onboarding/draft         Cleanup — called automatically on onboarding complete
POST   /api/v1/onboarding/funnel-event  Fire-and-forget wizard step telemetry
GET    /api/v1/onboarding/funnel-report Aggregate funnel counts per step
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import onboarding_drafts, onboarding_funnel_events
from app.models.onboarding_draft import DraftResponse, SaveDraftBody
from app.models.onboarding_funnel import LogFunnelEventBody

router = APIRouter()


# ── Internal helper ────────────────────────────────────────────────────────────

def _doc_to_draft_response(doc: dict) -> DraftResponse:
    """Convert a raw MongoDB onboarding_draft document to DraftResponse."""
    return DraftResponse(
        workspace_id=     doc.get("workspace_id"),
        brand_id=         doc.get("brand_id"),
        brand_type=       doc.get("brand_type"),
        current_step=     doc.get("current_step", 1),
        total_steps=      doc.get("total_steps", 7),
        is_complete=      doc.get("is_complete", False),
        identity=         doc.get("identity", {}),
        pillars_data=     doc.get("pillars_data"),
        icp_data=         doc.get("icp_data"),
        positioning_data= doc.get("positioning_data"),
        audience=         doc.get("audience", {}),
        voice_tone=       doc.get("voice_tone", {}),
        setup_path=       doc.get("setup_path"),
        extraction_data=  doc.get("extraction_data"),
        manual_data=      doc.get("manual_data"),
        platforms=        doc.get("platforms", []),
        completed_steps=  doc.get("completed_steps", []),
        blueprint_version=doc.get("blueprint_version", "2.0"),
        updated_at=       doc["updated_at"],
    )


# ── POST /api/v1/onboarding/draft ─────────────────────────────────────────────

@router.post("/draft", response_model=DraftResponse, status_code=200)
@limiter.limit("60/minute")
async def save_draft(
    request: Request,
    body: SaveDraftBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> DraftResponse:
    """
    Upsert the onboarding draft for the caller within the active workspace.

    Called debounced on every state change during onboarding.
    One document per (workspace, user) — always overwritten with latest state.
    Full state sent every time (last-write-wins, no partial merges).
    brand_id will be null on the first save and populated from step 2 onward.
    """
    now = datetime.now(timezone.utc)

    doc = await onboarding_drafts.find_one_and_update(
        {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id},
        {
            "$set": {
                "workspace_id":     ctx.workspace_id,
                "user_id":          ctx.user_id,
                "brand_id":         body.brand_id,
                "brand_type":       body.brand_type,
                "current_step":     body.current_step,
                "total_steps":      body.total_steps,
                "is_complete":      body.is_complete,
                "identity":         body.identity,
                "pillars_data":     body.pillars_data,
                "icp_data":         body.icp_data,
                "positioning_data": body.positioning_data,
                "audience":         body.audience,
                "voice_tone":       body.voice_tone,
                "setup_path":       body.setup_path,
                "extraction_data":  body.extraction_data,
                "manual_data":      body.manual_data,
                "platforms":        body.platforms,
                "completed_steps":  body.completed_steps,
                "blueprint_version":body.blueprint_version,
                "updated_at":       now,
            },
            "$setOnInsert": {
                "created_at": now,
            },
        },
        upsert=True,
        return_document=True,
    )

    return _doc_to_draft_response(doc)


# ── GET /api/v1/onboarding/draft ──────────────────────────────────────────────

@router.get("/draft", response_model=DraftResponse)
@limiter.limit("30/minute")
async def get_draft(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> DraftResponse:
    """
    Fetch the current user's active onboarding draft.

    Returns 404 with detail "No active draft" when:
    - User has never started onboarding
    - Onboarding was completed and draft was deleted

    The frontend MUST treat 404 as a clean state, not an error.
    Do not show an error toast on 404 from this endpoint.
    """
    doc = await onboarding_drafts.find_one(
        {
            "workspace_id": ctx.workspace_id,
            "user_id":      ctx.user_id,
            "is_complete":  False,
        }
    )

    if not doc:
        raise HTTPException(status_code=404, detail="No active draft")

    return _doc_to_draft_response(doc)



@router.delete("/draft", status_code=204)
@limiter.limit("20/minute")
async def delete_draft(
    request: Request,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> None:
    """
    Delete the caller's onboarding draft in the active workspace.

    Idempotent — no error raised if no draft exists.
    """
    await onboarding_drafts.delete_one(
        {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id}
    )


# ── POST /api/v1/onboarding/funnel-event ──────────────────────────────────────

@router.post("/funnel-event", status_code=202)
@limiter.limit("120/minute")
async def log_funnel_event(
    request: Request,
    body: LogFunnelEventBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> dict[str, str]:
    """
    Record one onboarding-wizard funnel event (step_reached or completed).

    Fire-and-forget telemetry — never blocks or fails the wizard itself; the
    frontend doesn't wait on this call before advancing. Answers "which step
    do people actually stall at" with real data instead of persona-based
    guesswork. See GET /funnel-report to read it back.
    """
    await onboarding_funnel_events.insert_one({
        "id": str(uuid4()),
        "workspace_id": ctx.workspace_id,
        "user_id": ctx.user_id,
        "brand_id": body.brand_id,
        "brand_type": body.brand_type,
        "event": body.event.value,
        "step": body.step,
        "step_title": body.step_title,
        "total_steps": body.total_steps,
        "created_at": datetime.now(timezone.utc),
    })
    return {"status": "logged"}


# ── GET /api/v1/onboarding/funnel-report ──────────────────────────────────────

@router.get("/funnel-report")
@limiter.limit("30/minute")
async def get_funnel_report(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """
    Aggregate funnel-event counts for the caller's active workspace over the
    last `days` days: how many step_reached events landed at each step, and
    how many completed. Scoped to the active workspace, same as every other
    read in this app — there's no cross-workspace admin surface to aggregate
    globally from.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    match: dict[str, Any] = {"workspace_id": ctx.workspace_id, "created_at": {"$gte": since}}

    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": {"event": "$event", "step": "$step", "step_title": "$step_title"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"_id.step": 1}},
    ]
    rows = await onboarding_funnel_events.aggregate(pipeline).to_list(length=500)

    started = await onboarding_funnel_events.count_documents(
        {**match, "event": "step_reached", "step": 1}
    )
    completed = await onboarding_funnel_events.count_documents(
        {**match, "event": "completed"}
    )

    return {
        "window_days": days,
        "started": started,
        "completed": completed,
        "completion_rate": round(completed / started, 3) if started else None,
        "by_step": [
            {
                "event": r["_id"]["event"],
                "step": r["_id"]["step"],
                "step_title": r["_id"]["step_title"],
                "count": r["count"],
            }
            for r in rows
        ],
    }