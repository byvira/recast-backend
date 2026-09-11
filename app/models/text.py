"""Pydantic models for the text content pipeline — aligned with frontend ConfigPanel."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field
from enum import Enum as PyEnum

# ─────────────────────────────────────────────────────────────────────────────
# LANGUAGE — ISO 639-1 → display-name table for languages this codebase has a
# curated display name for. This is NOT an allowlist: `language` fields below
# take a raw, opaque string with zero validation against this or any other
# fixed set (see app/pipelines/text/generator.py's resolve_language_name(),
# which passes an unrecognised code straight to the LLM rather than rejecting
# or silently substituting English). LANGUAGE_NAMES exists only to give a
# handful of well-known codes a nicer instruction-prompt name than the bare
# code would read as.
# ─────────────────────────────────────────────────────────────────────────────
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ta": "Tamil",
    "hi": "Hindi",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "ar": "Arabic",
    "ja": "Japanese",
    "zh": "Chinese",
    "id": "Indonesian",
    "vi": "Vietnamese",
    "th": "Thai",
    "bn": "Bengali",
    "te": "Telugu",
    "mr": "Marathi",
    "ur": "Urdu",
    "tr": "Turkish",
    "ru": "Russian",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "sw": "Swahili",
}


class Platform(str, Enum):
    LINKEDIN = "LinkedIn"
    TWITTER = "Twitter/X"
    TWITTER_THREAD = "Twitter/X Thread"
    INSTAGRAM = "Instagram"
    FACEBOOK = "Facebook"
    BLOG = "Blog"
    NEWSLETTER = "Newsletter"
    YOUTUBE = "YouTube"

class InputSourceType(str, Enum):
    TEXT = "text"          # write mode
    TOPIC = "topic"        # prompt mode
    URL = "url"            # url mod
    TRANSCRIPT = "transcript"  # from audio/video pipeline


class ContentIntent(str, Enum):
    POST = "post"
    THREAD = "thread"
    BLOG = "blog"
    NEWSLETTER = "newsletter"
    CAPTION = "caption"
    DESCRIPTION = "description"
    AUTO = "auto"


class ContentGoal(str, Enum):
    """Maps to ConfigPanel ContentGoalSelector options."""
    EDUCATE = "educate"
    PROMOTE = "promote"
    ENTERTAIN = "entertain"
    INSPIRE = "inspire"
    ANNOUNCE = "announce"
    ENGAGE = "engage"
    CONVERT = "convert"


class ToneOverride(str, Enum):
    """Maps to ConfigPanel ToneSelector options.
    BRAND means use the user's brand voice profile — no override.
    All other values override the brand tone for this generation only.
    """
    BRAND = "brand"
    FORMAL = "formal"
    CASUAL = "casual"
    PUNCHY = "punchy"
    STORYTELLING = "storytelling"


class ScheduleMode(str, Enum):
    NOW = "now"
    SCHEDULED = "scheduled"


class ExtrasConfig(BaseModel):
    """Maps directly to ConfigPanel ExtrasToggles."""
    hook_variations: bool = True
    hashtags: bool = True
    auto_cta: bool = False
    seo_meta: bool = False
    grammar_check: bool = False
    plagiarism_check: bool = False
    avoid_blacklist: bool = True
    pdf_export: bool = False


class GenerateTextRequest(BaseModel):
    """
    API request body — every field maps to a ConfigPanel control.
    Frontend sends this on Run button click.
    """
    # Input section
    source_type: InputSourceType
    content: str = Field(..., min_length=1)

    # Content targeting
    platforms: list[Platform] = Field(..., min_length=1)
    brand_id: str

    # Content intent and style
    intent: ContentIntent = ContentIntent.AUTO
    goal: Optional[ContentGoal] = None
    tone: ToneOverride = ToneOverride.BRAND

    # Extras — maps to ExtrasToggles
    extras: ExtrasConfig = ExtrasConfig()

    # Publish targets — maps to PlatformTargets
    publish_targets: list[str] = []

    # Scheduling — maps to ScheduleSelector
    schedule_mode: ScheduleMode = ScheduleMode.NOW
    scheduled_at: Optional[datetime] = None

    # Misc
    # None = caller expressed no preference for this request — the precedence
    # chain in app.shared.language falls through to the workspace's default,
    # then the caller's own account default, then "en". A non-None value here
    # is a per-request override and wins outright; it is never validated
    # against a fixed set.
    language: Optional[str] = None
    batch_mode: bool = False
    batch_days: int = Field(7, ge=1, le=30)


class RepurposeRequest(BaseModel):
    """
    Repurpose mode — maps to RepurposeInput tab in ConfigPanel.
    """
    source_content: str
    source_platform: Platform
    target_platforms: list[Platform]
    brand_id: str
    goal: Optional[ContentGoal] = None
    tone: ToneOverride = ToneOverride.BRAND
    extras: ExtrasConfig = ExtrasConfig()
    language: Optional[str] = None  # see GenerateTextRequest.language


class NormalisedInput(BaseModel):
    """Internal model — all input types converge to this before pipeline runs."""
    source_type: InputSourceType
    raw_content: str
    detected_intent: ContentIntent
    workspace_id: str = ""
    user_id: str
    brand_id: str
    target_platforms: list[Platform]
    session_id: str
    language: str = "en"


class AgentTask(BaseModel):
    """A single task dispatched to a specialist agent node."""
    agent: str
    platform: Optional[Platform] = None
    content: str
    brand_context: str
    session_id: str
    retry_count: int = 0
    metadata: dict[str, Any] = {}


class AgentResult(BaseModel):
    """Result returned by any specialist agent."""
    agent: str
    platform: Optional[Platform] = None
    output: dict
    success: bool = True
    error: Optional[str] = None


class HookVariant(BaseModel):
    text: str
    style: str
    score: int


class SEOPackage(BaseModel):
    title: str
    meta_description: str
    primary_keyword: str
    secondary_keywords: list[str]
    hashtags: list[str]
    slug: str
    tags: list[str]


class QualityResult(BaseModel):
    passed: bool
    issues: list[str]
    content: str
    readability_score: Optional[float] = None


class GeneratedPiece(BaseModel):
    """A single generated content piece for one platform."""
    platform: Platform
    workspace_id: str = ""
    content: str
    word_count: int
    char_count: int
    hooks: list[dict] = []
    seo: dict = {}
    quality_passed: bool = True
    quality_issues: list[str] = []
    readability_score: Optional[float] = None 
    flagged_for_review: bool = False
    repurposed: bool = False
    readability_score: Optional[float] = None  
    # Publish fields — populated when publish_targets are set
    publish_target: Optional[str] = None
    publish_status: Optional[str] = None
    publish_scheduled_at: Optional[datetime] = None
    publish_job_id: Optional[str] = None


class TextPipelineResult(BaseModel):
    """Full result returned to the frontend."""
    session_id: str
    workspace_id: str = ""
    user_id: str
    brand_id: str
    pieces: list[GeneratedPiece]
    source_type: InputSourceType
    schedule_mode: str = "now"
    scheduled_at: Optional[datetime] = None
    batch_mode: bool = False
    created_at: datetime
    pdf_export_url: Optional[str] = None
    batch_job_id: Optional[str] = None
    # Layer-1 personal assistant: a cached, LLM-free voice-alignment read for the
    # caller. Populated behind a 150 ms timeout — None if the read is slow or the
    # member has no persona yet. Never blocks generation. See app/agents/personal.
    assistant_nudge: Optional[dict] = None

class BatchGenerateRequest(BaseModel):
    topic_cluster: str
    platforms: list[Platform]
    brand_id: str
    extras: ExtrasConfig = ExtrasConfig()
    days: int = Field(7, ge=1, le=30)
    detected_intent: ContentIntent = ContentIntent.AUTO
    language: Optional[str] = None  # see GenerateTextRequest.language


class ApprovalStatus(str, PyEnum):
    PENDING   = "pending"
    APPROVED  = "approved"
    REJECTED  = "rejected"


class PublishStatus(str, PyEnum):
    PENDING    = "pending"
    SCHEDULED  = "scheduled"
    PUBLISHED  = "published"
    FAILED     = "failed"


class ContentSession(BaseModel):
    """
    One session = one API call to /generate, /repurpose, or /batch.
    Contains metadata about the request. Pieces are stored separately.
    """
    session_id: str
    workspace_id: str = ""
    user_id: str
    brand_id: str
    source_type: str
    platforms: list[str]
    goal: Optional[str] = None
    tone: Optional[str] = None
    batch_mode: bool = False
    batch_day_index: Optional[int] = None
    pieces_count: int = 0
    is_repurpose: bool = False
    schedule_mode: str = "now"
    scheduled_at: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ContentPiece(BaseModel):
    """
    One piece = one platform output from a session.
    Stores the full generated content plus all quality metadata.
    """
    piece_id: str
    session_id: str
    workspace_id: str = ""
    user_id: str
    brand_id: str
    platform: str
    content: str
    word_count: int = 0
    char_count: int = 0
    hooks: list[dict] = []
    seo: dict = {}
    quality_passed: bool = True
    quality_issues: list[str] = []
    flagged_for_review: bool = False
    readability_score: Optional[float] = None
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    repurposed: bool = False
    publish_status: PublishStatus = PublishStatus.PENDING
    publish_scheduled_at: Optional[str] = None
    publish_target: Optional[str] = None
    publish_job_id: Optional[str] = None
    version_count: int = 1
    created_at: datetime
    updated_at: datetime


class ContentPieceVersion(BaseModel):
    """
    One version = one snapshot of a piece's content.
    Version 1 is always the original generated content.
    Version N is created on every chip application or chat refinement.
    """
    version_id: str
    piece_id: str
    session_id: str
    workspace_id: str = ""
    user_id: str
    version_number: int
    content: str
    word_count: int
    char_count: int
    action: str        # "original", "make_punchier", "shorten", "chat_turn_1" etc
    instruction: str   
    platform: str
    created_at: datetime


     
class RegenerateRequest(BaseModel):
    """Request body for single-platform content regeneration."""
    platform:  str
    brand_id:  str
    piece_id:  Optional[str] = None   # used to pull original source content
    content:   Optional[str] = None   # fallback if piece_id not provided
    tone:      Optional[str] = "brand"
    goal:      Optional[str] = None
 
 
class RegenerateResponse(BaseModel):
    platform:          str
    content:           str
    hook_score:        int  = 0
    readability_score: int  = 0
    readability_level: str  = "Standard"
    piece_id:          str  = ""
    hashtags:          list[str] = []
    hook_alternatives: list[str] = []