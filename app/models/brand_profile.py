"""Pydantic models for brand profile onboarding and CRUD operations."""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class BrandType(str, Enum):
    """Category of brand being set up."""

    PERSON = "Person"
    PERSONAL_BRAND = "Personal Brand"
    BUSINESS = "Business"
    PRODUCT = "Product"


class ReadingLevel(str, Enum):
    SIMPLIFIED = "Simplified"
    STANDARD = "Standard"
    EXPERT = "Expert"


class KnowledgeBase(str, Enum):
    BEGINNER = "Beginner"
    INTERMEDIATE = "Intermediate"
    ADVANCED = "Advanced"


class HumorLevel(str, Enum):
    NONE = "None"
    SUBTLE = "Subtle"
    FREQUENT = "Frequent"


class EmojiUsage(str, Enum):
    NEVER = "Never"
    SOMETIMES = "Sometimes"
    OFTEN = "Often"


class SetupPath(str, Enum):
    EXTRACT = "extract"
    MANUAL = "manual"


class PhrasePlacement(str, Enum):
    HOOK = "hook"
    TRANSITION = "transition"
    CLOSE = "close"
    ANY = "any"


class Phrase(BaseModel):
    """A signature phrase with its intended placement in content."""

    text: str
    placement: PhrasePlacement


class AudienceProfile(BaseModel):
    """Target audience characteristics for content calibration."""

    reading_level: ReadingLevel = ReadingLevel.STANDARD
    knowledge_base: KnowledgeBase = KnowledgeBase.INTERMEDIATE
    primary_pain_point: str = ""
    extra: dict = {}


class VoiceTone(BaseModel):
    """Brand voice and stylistic tone settings."""

    tones: list[str] = []
    humor: HumorLevel = HumorLevel.NONE
    emoji: EmojiUsage = EmojiUsage.SOMETIMES
    style: str = ""


class ExtractionData(BaseModel):
    """Data collected during the AI extraction onboarding path."""

    files: list[dict] = []
    urls: list[str] = []
    connected_accounts: list[dict] = []
    confidence_score: int = Field(0, ge=0, le=100)
    extracted_samples: list[str] = []


class ManualData(BaseModel):
    """Data collected during the manual onboarding path."""

    openers: list[str] = []
    closers: list[str] = []
    phrases: list[Phrase] = []
    banned_words: list[str] = []
    preferred_synonyms: list[dict] = []


class PersonIdentity(BaseModel):
    """Identity fields for a Person brand type."""

    name: str
    type: BrandType = BrandType.PERSON
    headline: str = ""
    bio: str = ""
    location: str = ""
    website: str = ""
    goals: list[str] = []


class PersonalBrandIdentity(BaseModel):
    """Identity fields for a Personal Brand type."""

    name: str
    type: BrandType = BrandType.PERSONAL_BRAND
    niche: str = ""
    tagline: str = ""
    core_message: str = ""
    content_pillars: list[str] = []
    monetization: list[str] = []


class BusinessIdentity(BaseModel):
    """Identity fields for a Business brand type."""

    name: str
    type: BrandType = BrandType.BUSINESS
    industry: str = ""
    tagline: str = ""
    mission: str = ""
    company_size: str = ""
    target_market: str = ""
    competitors: list[str] = []


class ProductIdentity(BaseModel):
    """Identity fields for a Product brand type."""

    name: str
    type: BrandType = BrandType.PRODUCT
    category: str = ""
    one_liner: str = ""
    problem_solved: str = ""
    key_features: list[str] = []
    pricing: str = ""
    stage: str = ""


class StepData(BaseModel):
    """Flexible step data container — validated per brand_type in route handler."""

    data: dict = {}


class CreateBrandProfileBody(BaseModel):
    """Request body to initialise a new brand profile."""

    brand_type: BrandType


class SaveStepBody(BaseModel):
    """Request body for saving a single onboarding step."""

    step: int = Field(..., ge=1, le=10)
    data: dict


class UpdateVoiceBody(BaseModel):
    """Request body for editing tone/vocabulary directly, outside the
    onboarding step sequence (#9c — inline editing on the Voice Blueprint
    view). Each field is set independently ($set only what's provided) —
    unlike PUT /{id}/step's "setup" step, which overwrites manual_data
    wholesale alongside extraction_data and setup_path from the same
    payload, this can't accidentally null out sibling fields the caller
    didn't mean to touch.
    """

    voice_tone: VoiceTone | None = None
    manual_data: ManualData | None = None


class VoiceCalibration(BaseModel):
    """My Voices' Calibration tab — a separate, more granular tone-tuning
    surface than voice_tone (which drives real generation prompts
    directly). Deliberately its own model rather than folded into
    VoiceTone: e.g. emoji_usage here is a 4-way UI preference
    (none/minimal/bullets/expressive), distinct from voice_tone.emoji's
    3-way generation instruction (Never/Sometimes/Often) — conflating
    them would make one UI silently override the other's meaning.
    Saved as one unit (matches the page's own single "Save Voice
    Settings" button), not field-by-field like UpdateVoiceBody.
    """

    formality: int = Field(50, ge=0, le=100)
    directness: int = Field(50, ge=0, le=100)
    humor: int = Field(50, ge=0, le=100)
    optimism: int = Field(50, ge=0, le=100)
    energy: int = Field(50, ge=0, le=100)
    sentence_length: Literal["short", "balanced", "flowing"] = "balanced"
    paragraph_spacing: Literal["single", "double", "dense"] = "single"
    vocabulary_level: Literal["simple", "technical", "academic"] = "simple"
    hook_aggressiveness: int = Field(50, ge=0, le=100)
    emoji_usage: Literal["none", "minimal", "bullets", "expressive"] = "minimal"
    allow_em_dashes: bool = True
    allow_ellipses: bool = False
    use_lowercase_bullets: bool = True
    channel_rules: dict[str, str] = Field(default_factory=dict)
    signature_phrases: list[str] = Field(default_factory=list)


class TrainingSample(BaseModel):
    """My Voices' Training tab — a writing sample the user pasted in to
    teach this voice. extracted_traits stays empty until real trait
    analysis exists (an LLM call, not built here) — the original mock
    always showed 3 fixed fake traits regardless of content; an empty
    list is more honest than fabricating that analysis."""

    id: str
    title: str
    source_type: Literal["post", "newsletter", "transcript", "notes"]
    word_count: int
    snippet: str
    extracted_traits: list[str] = Field(default_factory=list)
    added_at: datetime


class UpdateCalibrationBody(BaseModel):
    """Full replace — matches the Calibration tab's single 'Save Voice
    Settings' button saving everything at once, not incremental
    per-field patches."""

    calibration: VoiceCalibration


class AddTrainingSampleBody(BaseModel):
    title: str
    source_type: Literal["post", "newsletter", "transcript", "notes"]
    content: str


class PreviewRewriteBody(BaseModel):
    """My Voices' Playground tab — rewrite arbitrary sample text in this
    brand's real voice. Read-only: never persists anything."""

    sample_text: str


class PreviewRewriteResponse(BaseModel):
    rewritten: str
    # The model's own estimate of how closely `rewritten` matches the
    # brand voice profile — real per-input variation (unlike the old
    # Playground mock, which showed a fixed 98.2% regardless of input),
    # but still a self-assessment, not a rigorous, independently
    # verified metric.
    tone_match_score: int


class BrandProfile(BaseModel):
    """Full brand profile document as stored in MongoDB."""

    id: str
    workspace_id: str = ""       # owning workspace — primary scoping key
    user_id: str                 # creator (audit / created_by), no longer the scoping key
    brand_type: BrandType
    identity: dict = {}
    audience: AudienceProfile = AudienceProfile()
    voice_tone: VoiceTone = VoiceTone()
    setup_path: Optional[SetupPath] = None
    extraction_data: Optional[ExtractionData] = None
    manual_data: Optional[ManualData] = None
    # Step 3's type-specific answers (Personal Brand/Business/Product only —
    # Person has no step 3 of this kind). _build_step_update() in
    # app/api/v1/brand.py already writes these into the Mongo document; they
    # were missing here entirely, so _doc_to_brand_profile() silently
    # dropped them from every API response even though they were saved —
    # the onboarding wizard's resume flow had no way to see them.
    pillars_data: Optional[dict] = None
    icp_data: Optional[dict] = None
    positioning_data: Optional[dict] = None
    completed_steps: list[str] = []
    platforms: list[str] = []
    blueprint_version: str = "2.0"
    is_complete: bool = False
    onboarding_step: int = 1
    # My Voices page — one workspace-wide default brand voice. No workspace
    # ever has more than one is_default=True brand (see set_default_brand
    # in app/api/v1/brand.py, which clears every sibling atomically).
    is_default: bool = False
    calibration: VoiceCalibration = Field(default_factory=VoiceCalibration)
    training_samples: list[TrainingSample] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
