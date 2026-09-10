"""Brand profile CRUD routes — create, list, read, update steps, complete, delete.

Scoped to the caller's active workspace (``X-Workspace-Id`` header or default).
Writes require the ``edit_brand_voice`` permission; reads require membership only.
``user_id`` on each document is the creator (audit), not the scoping key.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import brand_profiles, users
from app.models.brand_profile import (
    BrandProfile,
    BrandType,
    CreateBrandProfileBody,
    SaveStepBody,
)

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
    from app.models.brand_profile import AudienceProfile, VoiceTone

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
        platforms=doc.get("platforms", []),
        blueprint_version=doc.get("blueprint_version", "2.0"),
        is_complete=doc.get("is_complete", False),
        onboarding_step=doc.get("onboarding_step", 1),
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

    return {"brand_id": brand_id, "is_complete": True}


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
