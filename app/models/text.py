"""Pydantic models for the text content pipeline — aligned with frontend ConfigPanel."""

import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from enum import Enum as PyEnum

# Em dashes are a well-known LLM writing tell users flagged repeatedly as
# "AI slop" — prompt instructions alone don't reliably stop models from
# using them, so GeneratedPiece strips them unconditionally below. Matches
# with surrounding whitespace so "great—impactful" and "great — impactful"
# both collapse to a single ", " rather than losing the word boundary.
_EM_DASH_RE = re.compile(r"\s*—\s*")


def strip_em_dashes(text: str) -> str:
    if "—" not in text:
        return text
    cleaned = _EM_DASH_RE.sub(", ", text)
    cleaned = re.sub(r",\s*,", ",", cleaned)          # "foo, , bar" → "foo, bar"
    cleaned = re.sub(r"\s+([,.!?])", r"\1", cleaned)   # no space before punctuation
    return cleaned.strip()

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
    # Standalone entries needed so build_language_instruction()'s mixed-mode
    # branch can resolve each half of a "+" code independently (e.g. "kn+en"
    # splits into "kn" and "en", each resolved separately) — these three
    # were missing even though the frontend's constants/languages.ts already
    # offers them as regular single-language choices.
    "kn": "Kannada",
    "ml": "Malayalam",
    "tl": "Filipino",
    # Mixed-language (code-switched) modes — a deliberate blended register,
    # not a translation target. build_language_instruction() detects the
    # "+" and renders a dedicated instruction instead of "respond entirely
    # in X"; these display names are used wherever resolve_language_name()
    # is already called (e.g. build_tone_override()'s code-switching text).
    "ta+en": "Tamil and English (Tanglish)",
    "hi+en": "Hindi and English (Hinglish)",
    "te+en": "Telugu and English (Tenglish)",
    "kn+en": "Kannada and English (Kanglish)",
    "ml+en": "Malayalam and English (Manglish)",
    "bn+en": "Bengali and English (Benglish)",
    "es+en": "Spanish and English (Spanglish)",
    "tl+en": "Filipino and English (Taglish)",
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
    PROFESSIONAL = "professional"
    DIRECT = "direct"


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


class StructureRuleInput(BaseModel):
    """One section of an enforced generation template — mirrors Presets'
    PresetStepRule (app/models/preset.py) field-for-field, but kept as its
    own small model here rather than importing across modules, same
    precedent as the frontend's separate PresetStepRule/PresetStepRuleApi
    types. When a RepurposeRequest carries a list of these, each target
    platform's content is generated section-by-section with each
    section's char_limit mechanically enforced (one retry, then flagged),
    instead of the structure being folded into the source text as
    advisory prose the LLM may or may not follow."""
    section_name: str
    char_limit: int
    guidelines: str = ""


class RepurposeRequest(BaseModel):
    """
    Repurpose mode — maps to RepurposeInput tab in ConfigPanel and to
    Quick Recast (Library's "Recast Again").
    """
    source_content: str
    source_platform: Platform
    target_platforms: list[Platform]
    brand_id: str
    goal: Optional[ContentGoal] = None
    tone: ToneOverride = ToneOverride.BRAND
    extras: ExtrasConfig = ExtrasConfig()
    language: Optional[str] = None  # see GenerateTextRequest.language
    # "text" (default) or "url" — the handler used to hardcode "text"
    # unconditionally, so pasting a URL into Quick Recast never actually
    # scraped it (app.pipelines.text.scraper.scrape_url, already real and
    # used by the main pipeline's "url" input mode) — it just fed the bare
    # URL string to the LLM as if it were the source content.
    source_type: InputSourceType = InputSourceType.TEXT
    # Presets' "Generate with this preset" / Simulate — see StructureRuleInput.
    structure_rules: Optional[list[StructureRuleInput]] = None


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


# Feature 8 — Library's "Repurpose with 3 Fresh Angles" opened the generic
# Quick Recast modal (cross-platform repurposing), not an actual angle
# comparison — there was no real angle concept anywhere in the backend to
# preview (the old angle_used/angle_score fields were hardcoded
# "auto"/0 placeholders, never real). This is that real capability.
class AngleVariant(BaseModel):
    name: str
    rationale: str
    content: str

    @field_validator("content")
    @classmethod
    def _strip_em_dashes(cls, v: str) -> str:
        return strip_em_dashes(v)


class GenerateAnglesRequest(BaseModel):
    content: str
    platform: Platform
    brand_id: str
    piece_id: Optional[str] = None


class GenerateAnglesResponse(BaseModel):
    angles: list[AngleVariant]


# New repurpose flow — an "AI Suggestions" step between input and
# generation. Quick Recast used to jump straight from a raw paste to
# blind platform checkboxes with no read of the content itself; this is a
# read-only, cheap suggestion call (no persistence, no full rewrite) the
# user can accept or override before the real /repurpose call runs.
class SuggestRepurposeRequest(BaseModel):
    source_content: str
    source_platform: Platform
    brand_id: str
    source_type: InputSourceType = InputSourceType.TEXT


class RepurposeSuggestion(BaseModel):
    suggested_platforms: list[Platform]
    rationale: str
    suggested_tone: Optional[str] = None
    suggested_angle: Optional[str] = None


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


class GeneratedSection(BaseModel):
    """One enforced-template section of a generated piece — see
    StructureRuleInput. char_count is the real length of `content`;
    char_limit is copied from the request's structure rule so a reader can
    see the section stayed within budget without cross-referencing the
    original preset."""
    section_name: str
    content: str
    char_limit: int
    char_count: int


class GeneratedPiece(BaseModel):
    """A single generated content piece for one platform."""
    platform: Platform
    workspace_id: str = ""
    # Populated by the blocking /generate and /repurpose handlers after
    # _save_result() persists the piece — GeneratedPiece itself never knows
    # its own id (save_pipeline_result() generates it), so this stays None
    # until the caller fills it in from that call's real return value. A
    # frontend needs this to act on the piece afterward (approve/edit/
    # schedule) instead of it looking saved with nothing real to act on.
    piece_id: Optional[str] = None
    content: str
    # Set only when generated from an enforced structure_rules template —
    # `content` above is always the flattened join of these sections (every
    # existing consumer — publish, chip refine, hook/SEO scoring, frontend
    # cards — only ever reads `content`), this is the breakdown for anyone
    # who wants to see per-section results.
    sections: Optional[list[GeneratedSection]] = None
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

    @field_validator("content")
    @classmethod
    def _strip_em_dashes(cls, v: str) -> str:
        return strip_em_dashes(v)

    @model_validator(mode="after")
    def _sync_counts_to_content(self) -> "GeneratedPiece":
        # word_count/char_count are computed by callers from the pre-strip
        # content; keep them accurate against whatever content ends up on
        # the model (a no-op when there was nothing to strip).
        self.word_count = len(self.content.split())
        self.char_count = len(self.content)
        return self


class PreviewUrlRequest(BaseModel):
    url: str


class PreviewUrlResponse(BaseModel):
    # A "what will be scraped" preview for the URL input tab, shown before
    # the user commits to running the pipeline. title/snippet are None when
    # the page couldn't be fetched or had no extractable text — the
    # frontend shows "couldn't preview this page" rather than treating it
    # as fatal.
    title: Optional[str] = None
    snippet: Optional[str] = None
    word_count: int = 0


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
    batch_day_index: Optional[int] = None
    angle: Optional[str] = None
    # Set only for repurpose runs — the Platform this content originated
    # from, so it survives the round-trip into content_pieces instead of
    # being dropped after the prompt is built (see save_pipeline_result /
    # save_live_piece).
    source_platform: Optional[str] = None
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
    # "queued" is the real worker-recognized "scheduled and waiting to fire"
    # state (app/workers/scheduled_posts.py polls for exactly this value).
    # A prior "scheduled" value existed here too and both content.py's
    # /pieces/{id}/schedule and the generation-time schedule_mode path wrote
    # it instead of "queued" — pieces looked scheduled in the UI but the
    # worker never picked them up. Fixed to all write QUEUED; the member is
    # removed so nothing can regress to writing it again.
    PENDING    = "pending"
    QUEUED     = "queued"
    PUBLISHING = "publishing"
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
    # Set only when this piece was created via /repurpose — the Platform
    # the source content came from.
    source_platform: Optional[str] = None
    content: str
    sections: Optional[list[GeneratedSection]] = None
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
    # Set only for pieces generated via run_batch_pipeline (campaigns'
    # generate-next-batch) — which day of the batch produced this piece,
    # and the AI-planned angle used for that day. None for non-batch
    # generation/repurpose.
    batch_day_index: Optional[int] = None
    angle: Optional[str] = None
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