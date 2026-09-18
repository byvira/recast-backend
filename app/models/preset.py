"""Pydantic models for content presets — reusable structure templates.

The Presets page (Frontend/Recast/app/(dashboard)/dashboard/presets/) was
entirely frontend mock state (INITIAL_PRESETS) with no backend model, no
persistence, and no delete action at all — every create/edit/clone/save
only mutated local React state and vanished on reload. This is the real
backend for that page, translating its existing CreativePreset/
PresetStepRule TypeScript shape to snake_case, following the same
convention app.api.v1.brand's normalise_brand_keys() establishes for this
codebase's frontend/backend key-casing boundary.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PresetCategory(str, Enum):
    TEXT_THREAD = "text_thread"
    CAROUSEL = "carousel"
    AUDIO_BRIEF = "audio_brief"
    VIDEO_SCRIPT = "video_script"
    NEWSLETTER = "newsletter"


class PresetStepRule(BaseModel):
    step_index: int
    section_name: str
    char_limit: int
    guidelines: str = ""


class CreatePresetRequest(BaseModel):
    title: str
    category: PresetCategory
    category_label: str
    description: str = ""
    target_channels: list[str] = Field(default_factory=list)
    structure_rules: list[PresetStepRule] = Field(default_factory=list)
    voice_binding_id: Optional[str] = None
    default_hashtags: list[str] = Field(default_factory=list)
    hook_formula_example: str = ""


class UpdatePresetRequest(BaseModel):
    """Every field optional — PATCH sets only what's provided, same
    independent-field convention as PATCH /brand/{id}/voice."""
    title: Optional[str] = None
    category: Optional[PresetCategory] = None
    category_label: Optional[str] = None
    description: Optional[str] = None
    target_channels: Optional[list[str]] = None
    structure_rules: Optional[list[PresetStepRule]] = None
    voice_binding_id: Optional[str] = None
    default_hashtags: Optional[list[str]] = None
    hook_formula_example: Optional[str] = None


class Preset(BaseModel):
    id: str
    workspace_id: str
    brand_id: Optional[str] = None
    version: int = 1
    title: str
    category: PresetCategory
    category_label: str
    description: str = ""
    target_channels: list[str] = Field(default_factory=list)
    structure_rules: list[PresetStepRule] = Field(default_factory=list)
    voice_binding_id: Optional[str] = None
    default_hashtags: list[str] = Field(default_factory=list)
    hook_formula_example: str = ""
    usage_count: int = 0
    tone_score: int = 0
    is_system_default: bool = False
    created_by: str
    created_at: datetime
    updated_at: datetime
