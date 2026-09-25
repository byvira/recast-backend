"""Brand profile CRUD routes — create, list, read, update steps, complete, delete.

Scoped to the caller's active workspace (``X-Workspace-Id`` header or default).
Writes require the ``edit_brand_voice`` permission; reads require membership only.
``user_id`` on each document is the creator (audit), not the scoping key.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import brand_profiles, users
from app.models.brand_profile import (
    AddTrainingSampleBody,
    BrandProfile,
    BrandType,
    CreateBrandProfileBody,
    PreviewRewriteBody,
    PreviewRewriteResponse,
    SaveStepBody,
    SetActiveBrandBody,
    TrainingSample,
    UpdateBrandTypeBody,
    UpdateCalibrationBody,
    UpdateVisualIdentityBody,
    UpdateVoiceBody,
)
from app.pipelines.brand.voice_suggestions import generate_voice_pattern_suggestions
from app.pipelines.brand.voice_playground import preview_rewrite_in_voice
from app.pipelines.brand.trait_extraction import extract_sample_traits
from app.pipelines.media.image_generation import generate_brand_mascot

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_BRAND_PROFILES_PER_WORKSPACE = 10


def normalise_brand_keys(data: dict) -> dict:
    """
    Normalise camelCase frontend keys to snake_case before saving to MongoDB.
    Ensures all reads can use snake_case without dual fallback workaround.
    """
    key_map = {
        "bannedWords":        "banned_words",
        "preferredSynonyms":  "preferred_synonyms",
        "productName":        "product_name",
        "brandType":          "brand_type",
        "voiceTone":          "voice_tone",
        "manualData":         "manual_data",
        "primaryPainPoint":   "primary_pain_point",
        "readingLevel":       "reading_level",
        "knowledgeBase":      "knowledge_base",
        "buyingMotivations":  "buying_motivations",
        "valueMetrics":       "value_metrics",
        "companyName":        "company_name",
        "positioningData":    "positioning_data",
    }

    def _normalise(obj):
        if isinstance(obj, dict):
            return {
                key_map.get(k, k): _normalise(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_normalise(i) for i in obj]
        return obj

    return _normalise(data)


def _doc_to_brand_profile(doc: dict) -> BrandProfile:
    """Convert a raw MongoDB document to a BrandProfile model instance."""
    from app.models.brand_profile import AudienceProfile, VoiceCalibration, VoiceTone

    return BrandProfile(
        id=doc["id"],
        workspace_id=doc.get("workspace_id", ""),
        user_id=doc["user_id"],
        brand_type=doc["brand_type"],
        identity=doc.get("identity", {}),
        audience=AudienceProfile(**doc["audience"]) if doc.get("audience") else AudienceProfile(),
        voice_tone=VoiceTone(**doc["voice_tone"]) if doc.get("voice_tone") else VoiceTone(),
        setup_path=doc.get("setup_path"),
        extraction_data=doc.get("extraction_data"),
        manual_data=doc.get("manual_data"),
        pillars_data=doc.get("pillars_data"),
        icp_data=doc.get("icp_data"),
        positioning_data=doc.get("positioning_data"),
        completed_steps=doc.get("completed_steps", []),
        platforms=doc.get("platforms", []),
        blueprint_version=doc.get("blueprint_version", "2.0"),
        is_complete=doc.get("is_complete", False),
        onboarding_step=doc.get("onboarding_step", 1),
        is_default=doc.get("is_default", False),
        is_active=doc.get("is_active", True),
        default_tone=doc.get("default_tone"),
        calibration=VoiceCalibration(**doc["calibration"]) if doc.get("calibration") else VoiceCalibration(),
        training_samples=doc.get("training_samples", []),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def _get_step_mapping(brand_type: str) -> dict[str, int]:
    """
    Return step number → field name mapping for a given brand type.

    Person:         5 steps (2-6)
    Others:         6 steps (2-7)

    Person step map:
      2 → identity
      3 → audience
      4 → voice_tone
      5 → setup (setup_path + manual_data/extraction_data)
      6 → platforms

    Non-Person step map:
      2 → identity
      3 → type-specific (pillars/icp/positioning)
      4 → audience
      5 → voice_tone
      6 → setup (setup_path + manual_data/extraction_data)
      7 → platforms
    """
    is_person = brand_type == "Person"
    if is_person:
        return {
            "identity":   2,
            "audience":   3,
            "voice_tone": 4,
            "setup":      5,
            "platforms":  6,
        }
    else:
        return {
            "identity":   2,
            "type_specific": 3,
            "audience":   4,
            "voice_tone": 5,
            "setup":      6,
            "platforms":  7,
        }


def _build_step_update(
    step: int,
    data: dict,
    brand_type: str,
    setup_path: str | None,
) -> dict[str, Any]:
    """
    Build a MongoDB $set update dict for the given onboarding step.

    Maps frontend step numbers to correct MongoDB document fields.
    Handles Person (6 steps) and non-Person (7 steps) differently.

    Args:
        step: Frontend step number (2-7).
        data: Caller-supplied field data.
        brand_type: Brand type string (Person/Personal Brand/Business/Product).
        setup_path: Current brand profile setup_path for setup step routing.

    Returns:
        Flat dict suitable for use as a $set value.
    """
    mapping = _get_step_mapping(brand_type)
    update: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    is_person = brand_type == "Person"

    # Step 2 — always identity for all brand types
    if step == mapping["identity"]:
        update["identity"] = data
        return update

    # Step 3 — type-specific for non-Person, audience for Person
    if is_person and step == mapping["audience"]:
        update["audience"] = data
        return update

    if not is_person and step == mapping.get("type_specific", -1):
        # Personal Brand → pillars_data
        # Business       → icp_data
        # Product        → positioning_data
        if brand_type == "Personal Brand":
            update["pillars_data"] = data
        elif brand_type == "Business":
            update["icp_data"] = data
        elif brand_type == "Product":
            update["positioning_data"] = data
        return update

    # Step 4 (non-Person) — audience
    if not is_person and step == mapping["audience"]:
        update["audience"] = data
        return update

    # Voice tone step
    if step == mapping["voice_tone"]:
        update["voice_tone"] = data
        return update

    # Setup step — split into three top-level fields
    if step == mapping["setup"]:
        update["setup_path"] = data.get("setup_path")
        update["extraction_data"] = data.get("extraction_data")
        update["manual_data"] = data.get("manual_data")
        return update

    # Platforms step — always last step
    if step == mapping["platforms"]:
        update["platforms"] = data.get("platforms", [])
        return update

    # Fallback — merge into identity
    update["identity"] = data
    return update


@router.post("/", status_code=201)
@limiter.limit("20/minute")
async def create_brand_profile(
    request: Request,
    body: CreateBrandProfileBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> dict[str, str]:
    """
    Create a minimal brand profile in the caller's active workspace.
    Enforces a maximum of 10 brand profiles per workspace.
    """
    existing_count = await brand_profiles.count_documents({"workspace_id": ctx.workspace_id})
    if existing_count >= MAX_BRAND_PROFILES_PER_WORKSPACE:
        raise HTTPException(
            status_code=400,
            detail="Maximum of 10 brand profiles per workspace reached.",
        )

    now = datetime.now(timezone.utc)
    brand_id = str(uuid4())

    doc: dict[str, Any] = {
        "id": brand_id,
        "workspace_id": ctx.workspace_id,
        "user_id": ctx.user_id,          # creator (audit)
        "brand_type": body.brand_type.value,
        "identity": {},
        "audience": {},
        "voice_tone": {},
        "pillars_data": None,
        "icp_data": None,
        "positioning_data": None,
        "setup_path": None,
        "extraction_data": None,
        "manual_data": None,
        "platforms": [],
        "blueprint_version": "2.0",
        "is_complete": False,
        "onboarding_step": 1,
        "created_at": now,
        "updated_at": now,
    }

    await brand_profiles.insert_one(doc)

    return {"brand_profile_id": brand_id, "brand_type": body.brand_type.value}


@router.get("/")
@limiter.limit("100/minute")
async def list_brand_profiles(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """List the active workspace's brand profiles with pagination."""
    skip = (page - 1) * limit

    total = await brand_profiles.count_documents({"workspace_id": ctx.workspace_id})
    docs = (
        await brand_profiles.find({"workspace_id": ctx.workspace_id})
        .skip(skip)
        .limit(limit)
        .to_list(length=limit)
    )

    items = [_doc_to_brand_profile(d) for d in docs]

    return {
        "items": [item.model_dump() for item in items],
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": (skip + limit) < total,
    }


@router.get("/{brand_id}")
@limiter.limit("100/minute")
async def get_brand_profile(
    request: Request,
    brand_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """Fetch a single brand profile by ID within the active workspace."""
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    return _doc_to_brand_profile(doc).model_dump()


@router.put("/{brand_id}/step")
@limiter.limit("50/minute")
async def save_brand_step(
    request: Request,
    brand_id: str,
    body: SaveStepBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> dict[str, Any]:
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    brand_type = doc["brand_type"]
    normalised_data = normalise_brand_keys(body.data)

    step_update = _build_step_update(
        step=body.step,
        data=normalised_data,
        brand_type=brand_type,
        setup_path=doc.get("setup_path"),
    )

    if body.step > doc.get("onboarding_step", 1):
        step_update["onboarding_step"] = body.step

    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id}, {"$set": step_update}
    )

    if "voice_tone" in step_update:
        from app.shared.governance_events import emit_brand_voice_updated
        emit_brand_voice_updated(
            ctx.workspace_id, actor_user_id=ctx.user_id, actor_role=ctx.role,
            brand_id=brand_id, changed_fields=["voice_tone"],
            diff_summary=f"voice_tone step {body.step} saved",
        )

    return {
        "brand_id": brand_id,
        "step": body.step,
        "next_step": body.step + 1,
    }


@router.patch("/{brand_id}/type")
@limiter.limit("20/minute")
async def update_brand_type(
    request: Request,
    brand_id: str,
    body: UpdateBrandTypeBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> dict[str, Any]:
    """
    Corrects brand_type on a profile still mid-onboarding.

    The wizard's type-selection screen can be revisited before a profile is
    complete, and reuses the already-created brand_id rather than creating a
    second orphaned draft (see handleSelectBrandType's "existing brand"
    guard in app/(dashboard)/onboarding/brand-voice/page.tsx). Every
    downstream step keys off brand_type — _build_step_update's
    type-specific field routing (pillars_data/icp_data/positioning_data) and
    the frontend's display-name lookup (identity.company_name vs
    identity.product_name) — so re-picking a type without updating it here
    left profiles with e.g. brand_type "Product" but Business-shaped
    identity/icp data, showing as "Untitled Product" everywhere. Blocked
    once a profile is complete to avoid reshaping a real, in-use brand.
    """
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")
    if doc.get("is_complete"):
        raise HTTPException(status_code=400, detail="Can't change brand type on a completed profile.")

    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {"$set": {"brand_type": body.brand_type.value, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"brand_id": brand_id, "brand_type": body.brand_type.value}


@router.patch("/{brand_id}/voice")
@limiter.limit("30/minute")
async def update_brand_voice(
    request: Request,
    brand_id: str,
    body: UpdateVoiceBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> dict[str, Any]:
    """
    #9c — edit tone and vocabulary directly from the Voice Blueprint view,
    without routing back through the onboarding wizard. Each field is set
    independently; omitting one leaves it untouched (unlike PUT /step's
    "setup" step, which overwrites manual_data wholesale alongside
    extraction_data/setup_path from the same payload).
    """
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    update: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    changed_fields: list[str] = []
    if body.voice_tone is not None:
        update["voice_tone"] = body.voice_tone.model_dump()
        changed_fields.append("voice_tone")
    if body.manual_data is not None:
        update["manual_data"] = body.manual_data.model_dump()
        changed_fields.append("manual_data")
    if body.default_tone is not None:
        update["default_tone"] = body.default_tone.value
        changed_fields.append("default_tone")

    if not changed_fields:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id}, {"$set": update}
    )

    if "voice_tone" in changed_fields:
        from app.shared.governance_events import emit_brand_voice_updated
        emit_brand_voice_updated(
            ctx.workspace_id, actor_user_id=ctx.user_id, actor_role=ctx.role,
            brand_id=brand_id, changed_fields=changed_fields,
            diff_summary="voice/vocabulary edited inline",
        )

    updated_doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    return _doc_to_brand_profile(updated_doc).model_dump()


@router.post("/{brand_id}/suggest-voice-patterns")
@limiter.limit("10/minute")
async def suggest_voice_patterns(
    request: Request,
    brand_id: str,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> dict[str, Any]:
    """
    AI-draft a starting set of openers, closers, and signature phrases from
    the brand's already-saved identity/audience/voice_tone.

    Read-only with respect to the brand profile — this never writes
    anything; the caller reviews and edits, then saves through the normal
    PUT /{brand_id}/step call like any other manual entry. A failed or
    empty generation is a 502 with a plain-language message, never a 500 —
    this is a convenience on top of manual entry, not a dependency of it.
    """
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    if not doc.get("identity"):
        raise HTTPException(
            status_code=400,
            detail="Add a few identity details first — the more Recast knows, the better these suggestions will be.",
        )

    suggestions = await generate_voice_pattern_suggestions(doc)
    if not suggestions:
        raise HTTPException(
            status_code=502,
            detail="Couldn't generate suggestions right now. Please try again, or write your own below.",
        )

    return suggestions


@router.post("/{brand_id}/preview-rewrite", response_model=PreviewRewriteResponse)
@limiter.limit("20/minute")
async def preview_voice_rewrite(
    request: Request,
    brand_id: str,
    body: PreviewRewriteBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> PreviewRewriteResponse:
    """
    My Voices' Playground tab — rewrite arbitrary sample text in this
    brand's real voice, with a real per-input tone-match estimate (not the
    old mock's fixed 98.2% shown for every input). Read-only: never
    persists anything.
    """
    if not body.sample_text.strip():
        raise HTTPException(status_code=400, detail="Sample text cannot be empty.")

    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    result = await preview_rewrite_in_voice(doc, body.sample_text)
    if not result:
        raise HTTPException(
            status_code=502,
            detail="Couldn't generate a preview rewrite right now. Please try again.",
        )
    return PreviewRewriteResponse(**result)


@router.patch("/{brand_id}/calibration", response_model=BrandProfile)
@limiter.limit("30/minute")
async def update_brand_calibration(
    request: Request,
    brand_id: str,
    body: UpdateCalibrationBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> BrandProfile:
    """
    My Voices' Calibration tab — full replace, matching the page's single
    "Save Voice Settings" button saving everything at once.
    """
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {"$set": {
            "calibration": body.calibration.model_dump(),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    updated = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    return _doc_to_brand_profile(updated)


@router.patch("/{brand_id}/visual-identity", response_model=BrandProfile)
@limiter.limit("30/minute")
async def update_brand_visual_identity(
    request: Request,
    brand_id: str,
    body: UpdateVisualIdentityBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> BrandProfile:
    """
    My Voices' Brand Assets tab — full replace, same single-save-button
    convention as update_brand_calibration above.
    """
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {"$set": {
            "visual_identity": body.visual_identity.model_dump(),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    updated = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    return _doc_to_brand_profile(updated)


@router.post("/{brand_id}/training-samples", response_model=BrandProfile, status_code=201)
@limiter.limit("30/minute")
async def add_training_sample(
    request: Request,
    brand_id: str,
    body: AddTrainingSampleBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> BrandProfile:
    """My Voices' Training tab — append a real writing sample."""
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Sample content cannot be empty.")

    content = body.content.strip()
    # PAR-015: was always []. A failed/unusable extraction still saves the
    # sample with an honest empty list — never blocks the save itself.
    traits = await extract_sample_traits(content, workspace_id=ctx.workspace_id)
    sample = TrainingSample(
        id=str(uuid4()),
        title=body.title.strip() or "Untitled sample",
        source_type=body.source_type,
        word_count=len(content.split()),
        snippet=content[:180] + ("..." if len(content) > 180 else ""),
        extracted_traits=traits,
        added_at=datetime.now(timezone.utc),
    )
    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {
            "$push": {"training_samples": sample.model_dump()},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    updated = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    return _doc_to_brand_profile(updated)


@router.delete("/{brand_id}/training-samples/{sample_id}", response_model=BrandProfile)
@limiter.limit("30/minute")
async def delete_training_sample(
    request: Request,
    brand_id: str,
    sample_id: str,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> BrandProfile:
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {
            "$pull": {"training_samples": {"id": sample_id}},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    updated = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    return _doc_to_brand_profile(updated)


@router.patch("/{brand_id}/set-default", response_model=BrandProfile)
@limiter.limit("30/minute")
async def set_default_brand(
    request: Request,
    brand_id: str,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> BrandProfile:
    """My Voices' "Set as Default" — exactly one default brand per
    workspace; clears every sibling atomically rather than trusting the
    caller to have deselected the old one."""
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    now = datetime.now(timezone.utc)
    await brand_profiles.update_many(
        {"workspace_id": ctx.workspace_id, "id": {"$ne": brand_id}},
        {"$set": {"is_default": False, "updated_at": now}},
    )
    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {"$set": {"is_default": True, "updated_at": now}},
    )
    updated = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    return _doc_to_brand_profile(updated)


@router.patch("/{brand_id}/set-active", response_model=BrandProfile)
@limiter.limit("30/minute")
async def set_active_brand(
    request: Request,
    brand_id: str,
    body: SetActiveBrandBody,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> BrandProfile:
    """My Voices' on/off toggle. Unlike set-default this isn't exclusive —
    any number of brands can be active or inactive independently. Disabling
    never blocks generation; it only makes brand_context.py/.jinja skip this
    brand's specific voice and fall back to a generic natural one."""
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {"$set": {"is_active": body.is_active, "updated_at": datetime.now(timezone.utc)}},
    )
    updated = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    return _doc_to_brand_profile(updated)


async def _generate_and_save_mascot(brand_id: str, workspace_id: str, user_id: str) -> None:
    """Row 16 — fire-and-forget mascot generation. Runs after the response
    that triggered it has already returned (asyncio.create_task), so a
    slow or failed generation never blocks brand completion or the manual
    regenerate endpoint below. Re-reads the doc fresh rather than trusting
    a stale copy, since this runs after the caller's own response."""
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id})
    if not doc:
        return
    asset = await generate_brand_mascot(brand_profile=doc, workspace_id=workspace_id, user_id=user_id)
    if not asset:
        return
    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": workspace_id},
        {
            "$set": {
                "visual_identity.mascot_url": asset.url,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


@router.put("/{brand_id}/complete")
@limiter.limit("20/minute")
async def complete_brand_profile(
    request: Request,
    brand_id: str,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> dict[str, Any]:
    """
    Mark a brand profile as complete.
    Sets user.onboarding_done = true if this is the caller's first completed profile.

    Row 16 — fires mascot generation in the background right after
    completion (fire-and-forget, doesn't block this response — generation
    takes several real seconds across multiple gate calls + the actual
    image call). The mascot appears in the Brand Assets tab once it's
    ready, not synchronously here.
    """
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    now = datetime.now(timezone.utc)
    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {"$set": {"is_complete": True, "updated_at": now}},
    )

    # onboarding_done stays a per-user flag — first completed profile anywhere flips it.
    if not ctx.user.get("onboarding_done", False):
        await users.update_one(
            {"id": ctx.user_id},
            {"$set": {"onboarding_done": True}},
        )

    if not (doc.get("visual_identity") or {}).get("mascot_url"):
        asyncio.create_task(_generate_and_save_mascot(brand_id, ctx.workspace_id, ctx.user_id))

    return {"brand_id": brand_id, "is_complete": True}


@router.post("/{brand_id}/visual-identity/mascot/regenerate", response_model=BrandProfile)
@limiter.limit("10/minute")
async def regenerate_brand_mascot(
    request: Request,
    brand_id: str,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> BrandProfile:
    """Row 16 — Brand Assets tab's "Regenerate" button. Unlike the
    fire-and-forget trigger on /complete, this awaits and returns the
    result directly — the user explicitly asked for this one and is
    looking at a loading state waiting for it."""
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    asset = await generate_brand_mascot(brand_profile=doc, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    if not asset:
        raise HTTPException(
            status_code=502,
            detail="Couldn't generate a mascot right now. Try again in a moment.",
        )

    await brand_profiles.update_one(
        {"id": brand_id, "workspace_id": ctx.workspace_id},
        {
            "$set": {
                "visual_identity.mascot_url": asset.url,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    updated = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    return _doc_to_brand_profile(updated)


@router.delete("/{brand_id}")
@limiter.limit("20/minute")
async def delete_brand_profile(
    request: Request,
    brand_id: str,
    ctx: WorkspaceContext = Depends(require("edit_brand_voice")),
) -> dict[str, str]:
    """Delete a brand profile from the active workspace."""
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Brand profile not found.")

    await brand_profiles.delete_one({"id": brand_id, "workspace_id": ctx.workspace_id})

    return {"message": "Brand profile deleted."}
