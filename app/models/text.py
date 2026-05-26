"""Pydantic models for the text content pipeline — aligned with frontend ConfigPanel."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


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
    URL = "url"            # url mode
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
    language: str = "en"
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


class NormalisedInput(BaseModel):
    """Internal model — all input types converge to this before pipeline runs."""
    source_type: InputSourceType
    raw_content: str
    detected_intent: ContentIntent
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
    content: str
    word_count: int
    char_count: int
    hooks: list[dict] = []
    seo: dict = {}
    quality_passed: bool = True
    quality_issues: list[str] = []
    
    flagged_for_review: bool = False
    repurposed: bool = False
    # Publish fields — populated when publish_targets are set
    publish_target: Optional[str] = None
    publish_status: Optional[str] = None
    publish_scheduled_at: Optional[datetime] = None
    publish_job_id: Optional[str] = None


class TextPipelineResult(BaseModel):
    """Full result returned to the frontend."""
    session_id: str
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

class BatchGenerateRequest(BaseModel):
    topic_cluster: str
    platforms: list[Platform]
    brand_id: str
    extras: ExtrasConfig = ExtrasConfig()
    days: int = Field(7, ge=1, le=30)
    detected_intent: ContentIntent = ContentIntent.AUTO
