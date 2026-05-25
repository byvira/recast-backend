"""Onboarding draft routes — save progress and resume from any device.

Endpoints
---------
POST   /api/v1/onboarding/draft   Upsert draft on every state change
GET    /api/v1/onboarding/draft   Resume — returns 404 when no active draft (not an error)
DELETE /api/v1/onboarding/draft   Cleanup — called automatically on onboarding complete
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.db.mongo import onboarding_drafts
from app.models.onboarding_draft import DraftResponse, SaveDraftBody

router = APIRouter()


# ── Internal helper ────────────────────────────────────────────────────────────

def _doc_to_draft_response(doc: dict) -> DraftResponse:
    """Convert a raw MongoDB onboarding_draft document to DraftResponse."""
    return DraftResponse(
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
    current_user: dict = Depends(get_current_user),
) -> DraftResponse:
    """
    Upsert the onboarding draft for the current user.

    Called debounced on every state change during onboarding.
    One document per user — always overwritten with latest state.
    Full state sent every time (last-write-wins, no partial merges).
    brand_id will be null on the first save and populated from step 2 onward.
    """
    now = datetime.now(timezone.utc)

    doc = await onboarding_drafts.find_one_and_update(
        {"user_id": current_user["id"]},
        {
            "$set": {
                "user_id":          current_user["id"],
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
    current_user: dict = Depends(get_current_user),
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
            "user_id":     current_user["id"],
            "is_complete": False,
        }
    )

    if not doc:
        raise HTTPException(status_code=404, detail="No active draft")

    return _doc_to_draft_response(doc)



@router.delete("/draft", status_code=204)
@limiter.limit("20/minute")
async def delete_draft(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> None:
    """
    Delete the current user's onboarding draft.

    Also called automatically inside complete_brand_profile() in brand.py
    so the draft is gone the moment onboarding finishes — even if the
    frontend crashes before it can call this directly.

    Idempotent — no error raised if no draft exists.
    """
    await onboarding_drafts.delete_one({"user_id": current_user["id"]})