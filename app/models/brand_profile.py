"""Pydantic models for brand profile onboarding and CRUD operations."""

from datetime import datetime
from enum import Enum
from typing import Optional

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
    created_at: datetime
    updated_at: datetime
