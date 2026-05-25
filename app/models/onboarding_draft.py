"""Pydantic models for onboarding draft save and resume."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class SaveDraftBody(BaseModel):
    """
    Request body for POST /api/v1/onboarding/draft.

    Full onboarding state sent on every Next click.
    Last-write-wins — no partial merges, always the complete snapshot.
    brand_id is null until step 1 completes and the brand profile is created.
    """

    brand_id:          Optional[str]            = None
    brand_type:        Optional[str]            = None
    current_step:      int
    total_steps:       int
    is_complete:       bool                     = False

    # Step 2 — identity (shape varies per brand_type)
    identity:          dict[str, Any]           = {}

    # Step 3 — type-specific (non-Person brands only)
    pillars_data:      Optional[dict[str, Any]] = None   # Personal Brand
    icp_data:          Optional[dict[str, Any]] = None   # Business
    positioning_data:  Optional[dict[str, Any]] = None   # Product

    # Step 4 — audience
    audience:          dict[str, Any]           = {}

    # Step 5 — voice & tone
    voice_tone:        dict[str, Any]           = {}

    # Step 6 — setup path + ingestion data
    setup_path:        Optional[str]            = None
    extraction_data:   Optional[dict[str, Any]] = None
    manual_data:       Optional[dict[str, Any]] = None

    # Step 7 — platforms
    platforms:         list[str]                = []

    # Optional extras
    completed_steps:   list[str]                = []
    blueprint_version: str                      = "2.0"


class DraftResponse(BaseModel):
    """
    Response for GET /api/v1/onboarding/draft and POST /api/v1/onboarding/draft.

    Returns the full snapshot so the frontend can restore
    OnboardingState directly without any transformation.
    """

    brand_id:          Optional[str]
    brand_type:        Optional[str]
    current_step:      int
    total_steps:       int
    is_complete:       bool
    identity:          dict[str, Any]
    pillars_data:      Optional[dict[str, Any]]
    icp_data:          Optional[dict[str, Any]]
    positioning_data:  Optional[dict[str, Any]]
    audience:          dict[str, Any]
    voice_tone:        dict[str, Any]
    setup_path:        Optional[str]
    extraction_data:   Optional[dict[str, Any]]
    manual_data:       Optional[dict[str, Any]]
    platforms:         list[str]
    completed_steps:   list[str]
    blueprint_version: str
    updated_at:        datetime