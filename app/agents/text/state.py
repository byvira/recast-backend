"""
TextAgentState — single source of truth for the text agent graph.

Every node in the graph reads from this state and writes back to it.
No node passes data directly to another node — all communication
happens through state. This is the contract between every agent
in the text pipeline.
"""

from typing import Any, Optional
from typing_extensions import TypedDict

from app.models.text import (
    ContentIntent,
    ContentGoal,
    InputSourceType,
    Platform,
    ToneOverride,
)


class TextAgentState(TypedDict):
    """
    Complete state for the text generation agent graph.

    Lifecycle of this state:
      1. Populated by orchestrator before graph invocation
      2. normalise_node       → fills normalised_content, session_id, detected_intent
      3. build_context_node   → fills brand_context, goal_context, tone_override, content_brief
      4. generate_node        → fills generated_content
      5. hooks_node           → fills hooks, recommended_hook_index
      6. seo_node             → fills seo_package
      7. quality_check_node   → fills quality_passed, quality_issues, readability_score
      8. rewrite_node         → updates generated_content, retry_count, retry_feedback
         OR
         flag_node            → sets flagged_for_review True
      9. collect_output_node  → appends to pieces
    """

    # ─────────────────────────────────────────────────────────────
    # IDENTITY — set by orchestrator before graph runs, never mutated
    # ─────────────────────────────────────────────────────────────

    session_id: str
    user_id: str
    brand_id: str

    # ─────────────────────────────────────────────────────────────
    # RAW INPUT — set by orchestrator, read by normalise_node
    # ─────────────────────────────────────────────────────────────

    raw_input: str
    source_type: InputSourceType
    target_platforms: list[Platform]
    current_platform: Platform
    language: str

    # ─────────────────────────────────────────────────────────────
    # USER INTENT — set by orchestrator from request body
    # ─────────────────────────────────────────────────────────────

    intent: ContentIntent
    goal: Optional[ContentGoal]
    tone: ToneOverride

    # ─────────────────────────────────────────────────────────────
    # EXTRAS — maps to ConfigPanel ExtrasToggles
    # ─────────────────────────────────────────────────────────────

    extras: dict[str, Any]
    # {
    #   "hook_variations": bool,
    #   "hashtags": bool,
    #   "auto_cta": bool,
    #   "seo_meta": bool,
    #   "grammar_check": bool,
    #   "plagiarism_check": bool,
    #   "avoid_blacklist": bool,
    #   "pdf_export": bool,
    # }

    # ─────────────────────────────────────────────────────────────
    # NORMALISED CONTENT — populated by normalise_node
    # ─────────────────────────────────────────────────────────────

    normalised_content: str
    detected_intent: ContentIntent

    # ─────────────────────────────────────────────────────────────
    # BUILT CONTEXT — populated by build_context_node
    # ─────────────────────────────────────────────────────────────

    brand_context: str
    goal_context: str
    tone_override_text: str
    # Named tone_override_text to avoid collision with the tone field above
    # which holds the ToneOverride enum value.

    content_brief: str

    # ─────────────────────────────────────────────────────────────
    # GENERATION OUTPUT — populated by generate_node
    # ─────────────────────────────────────────────────────────────

    generated_content: str

    # ─────────────────────────────────────────────────────────────
    # HOOKS — populated by hooks_node
    # ─────────────────────────────────────────────────────────────

    hooks: list[dict]
    recommended_hook_index: int

    # ─────────────────────────────────────────────────────────────
    # SEO — populated by seo_node
    # ─────────────────────────────────────────────────────────────

    seo_package: dict
    # {
    #   "title": str,
    #   "meta_description": str,
    #   "primary_keyword": str,
    #   "secondary_keywords": list[str],
    #   "hashtags": list[str],
    #   "slug": str,
    #   "tags": list[str],
    # }

    # ─────────────────────────────────────────────────────────────
    # QUALITY — populated by quality_check_node
    # ─────────────────────────────────────────────────────────────

    quality_passed: bool
    quality_issues: list[str]
    readability_score: Optional[float]

    # ─────────────────────────────────────────────────────────────
    # RETRY — managed by rewrite_node
    # ─────────────────────────────────────────────────────────────

    retry_count: int
    retry_feedback: str

    # ─────────────────────────────────────────────────────────────
    # PUBLISH — set by orchestrator from request
    # ─────────────────────────────────────────────────────────────

    publish_target: Optional[str]
    schedule_mode: str
    scheduled_at: Optional[str]

    # ─────────────────────────────────────────────────────────────
    # FLAGS
    # ─────────────────────────────────────────────────────────────

    flagged_for_review: bool

    # ─────────────────────────────────────────────────────────────
    # OUTPUT ACCUMULATOR — appended to by collect_output_node
    # ─────────────────────────────────────────────────────────────

    pieces: list[dict]
    # Serialised GeneratedPiece dicts — GeneratedPiece.model_dump()
    # TypedDict cannot hold Pydantic models directly

    # ─────────────────────────────────────────────────────────────
    # ERROR TRACKING
    # ─────────────────────────────────────────────────────────────

    errors: list[str]

    # ─────────────────────────────────────────────────────────────
    # REPURPOSE — only set for repurpose mode runs
    # ─────────────────────────────────────────────────────────────

    is_repurpose: bool
    source_platform: Optional[Platform]

    # ─────────────────────────────────────────────────────────────
    # BATCH MODE — only set for batch runs
    # ─────────────────────────────────────────────────────────────

    batch_mode: bool
    batch_day_index: Optional[int]
    batch_angle: Optional[str]


def build_initial_state(
    user_id: str,
    brand_id: str,
    raw_input: str,
    source_type: InputSourceType,
    current_platform: Platform,
    target_platforms: list[Platform],
    extras: dict,
    intent: ContentIntent = ContentIntent.AUTO,
    goal: Optional[ContentGoal] = None,
    tone: ToneOverride = ToneOverride.BRAND,
    language: str = "en",
    publish_target: Optional[str] = None,
    schedule_mode: str = "now",
    scheduled_at: Optional[str] = None,
    is_repurpose: bool = False,
    source_platform: Optional[Platform] = None,
    batch_mode: bool = False,
    batch_day_index: Optional[int] = None,
    batch_angle: Optional[str] = None,
) -> TextAgentState:
    """
    Build a clean initial state for one platform graph run.
    Called by orchestrator once per platform before asyncio.gather.
    All agent-populated fields start empty or at safe defaults.
    Class must be defined above this function — Python reads top to bottom.
    """
    return TextAgentState(
        # Identity
        session_id="",
        user_id=user_id,
        brand_id=brand_id,

        # Raw input
        raw_input=raw_input,
        source_type=source_type,
        target_platforms=target_platforms,
        current_platform=current_platform,
        language=language,

        # User intent
        intent=intent,
        goal=goal,
        tone=tone,

        # Extras
        extras=extras,

        # Normalised content — empty until normalise_node runs
        normalised_content="",
        detected_intent=intent,

        # Built context — empty until build_context_node runs
        brand_context="",
        goal_context="",
        tone_override_text="",
        content_brief="",

        # Generation — empty until generate_node runs
        generated_content="",

        # Hooks — empty until hooks_node runs
        hooks=[],
        recommended_hook_index=0,

        # SEO — empty until seo_node runs
        seo_package={},

        # Quality — safe defaults until quality_check_node runs
        quality_passed=False,
        quality_issues=[],
        readability_score=None,

        # Retry — clean start
        retry_count=0,
        retry_feedback="",

        # Publish
        publish_target=publish_target,
        schedule_mode=schedule_mode,
        scheduled_at=scheduled_at,

        # Flags
        flagged_for_review=False,

        # Output
        pieces=[],

        # Errors
        errors=[],

        # Repurpose
        is_repurpose=is_repurpose,
        source_platform=source_platform,

        # Batch
        batch_mode=batch_mode,
        batch_day_index=batch_day_index,
        batch_angle=batch_angle,
    )