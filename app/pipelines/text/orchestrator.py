"""
Text pipeline orchestrator — single entry point for all text generation.
Called by API routes. Handles normal generation, repurpose mode, and batch mode.
Runs all platforms in parallel using asyncio.gather.
One auto-retry per platform on hard quality failure — handled inside the graph.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from app.agents.text.graph import build_single_platform_graph
from app.agents.text.state import build_initial_state
from app.db.mongo import brand_profiles
from app.models.text import (
    AgentTask,
    ContentGoal,
    GeneratedPiece,
    InputSourceType,
    NormalisedInput,
    ContentIntent,
    Platform,
    TextPipelineResult,
    ToneOverride,
)
from app.pipelines.text.brand_context import build_brand_context
from app.pipelines.text.normalizer import normalise_input
from app.pipelines.text.repurpose import run_repurpose_agent
from app.pipelines.text.generator import validate_content
from app.agents.text.nodes import _extract_enforcement_data
from app.pipelines.text.seo import run_seo_agent, should_run_seo
from app.pipelines.text.hook_agent import run_hook_agent, apply_recommended_hook

logger = logging.getLogger(__name__)

# Compile once at module load — reused for every request
_text_graph = build_single_platform_graph()


def _build_metadata(extras, goal=None, tone=None, language: str = "en") -> dict:
    """
    Build the metadata dict stored in TextAgentState.extras.
    Every node reads what it needs from state["extras"].
    Carries all toggle states, style overrides, and language.
    """
    return {
        "hook_variations": extras.hook_variations,
        "hashtags": extras.hashtags,
        "auto_cta": extras.auto_cta,
        "seo_meta": extras.seo_meta,
        "grammar_check": extras.grammar_check,
        "plagiarism_check": extras.plagiarism_check,
        "avoid_blacklist": extras.avoid_blacklist,
        "pdf_export": extras.pdf_export,
        "goal": goal.value if goal else None,
        "tone": tone.value if tone else "brand",
        "language": language,
    }


async def _run_single_platform(
    platform: Platform,
    normalised: NormalisedInput,
    brand_profile: dict,
    metadata: dict,
    publish_target: Optional[str] = None,
    schedule_mode: str = "now",
    scheduled_at=None,
    is_repurpose: bool = False,
    source_platform: Optional[Platform] = None,
    batch_mode: bool = False,
    batch_day_index: Optional[int] = None,
    batch_angle: Optional[str] = None,
) -> GeneratedPiece:
    """
    Invokes the LangGraph for a single platform.
    Uses build_initial_state — never constructs TextAgentState manually.
    All processing happens inside graph nodes — no direct pipeline calls here.
    """
    goal_value = metadata.get("goal")
    tone_value = metadata.get("tone", "brand")
    goal_enum = ContentGoal(goal_value) if goal_value else None
    tone_enum = ToneOverride(tone_value) if tone_value else ToneOverride.BRAND

    initial_state = build_initial_state(
        user_id=normalised.user_id,
        brand_id=normalised.brand_id,
        raw_input=normalised.raw_content,
        source_type=normalised.source_type,
        current_platform=platform,
        target_platforms=normalised.target_platforms,
        extras=metadata,
        intent=normalised.detected_intent,
        goal=goal_enum,
        tone=tone_enum,
        language=normalised.language,
        publish_target=publish_target,
        schedule_mode=schedule_mode,
        scheduled_at=str(scheduled_at) if scheduled_at else None,
        is_repurpose=is_repurpose,
        source_platform=source_platform,
        batch_mode=batch_mode,
        batch_day_index=batch_day_index,
        batch_angle=batch_angle,
    )

    try:
        final_state = await _text_graph.ainvoke(initial_state)
    except Exception as e:
        logger.error(
            "Graph invocation failed for %s session %s: %s",
            platform, normalised.session_id, e,
        )
        return GeneratedPiece(
            platform=platform,
            content="",
            word_count=0,
            char_count=0,
            quality_passed=False,
            quality_issues=[f"Graph error: {str(e)}"],
            flagged_for_review=True,
        )

    pieces = final_state.get("pieces", [])
    if pieces:
        piece = GeneratedPiece(**pieces[-1])
        # FIX Issue 13 — set repurposed flag from graph state
        piece.repurposed = final_state.get("is_repurpose", False)
        return piece

    logger.error(
        "Graph returned no pieces for %s session %s",
        platform, normalised.session_id,
    )
    return GeneratedPiece(
        platform=platform,
        content="",
        word_count=0,
        char_count=0,
        quality_passed=False,
        quality_issues=["Graph returned no output"],
        flagged_for_review=True,
    )


async def run_text_pipeline(
    source_type: InputSourceType,
    content: str,
    platforms: list[Platform],
    brand_id: str,
    user_id: str,
    extras,
    goal=None,
    tone=None,
    intent=None,
    language: str = "en",
    source_platform: Optional[Platform] = None,
    is_repurpose: bool = False,
    schedule_mode: str = "now",
    scheduled_at=None,
    batch_day_index: Optional[int] = None,
) -> TextPipelineResult:
    brand_profile = await brand_profiles.find_one({"id": brand_id})
    if not brand_profile:
        raise ValueError(f"Brand profile not found: {brand_id}")

    metadata = _build_metadata(extras, goal, tone, language=language)

    normalised = await normalise_input(
        source_type=source_type,
        content=content,
        platforms=platforms,
        user_id=user_id,
        brand_id=brand_id,
        language=language,
        intent=intent,
    )

    if is_repurpose and source_platform:
        brand_context = build_brand_context(brand_profile)
        enforcement = _extract_enforcement_data(brand_profile)

        repurpose_coros = [
            run_repurpose_agent(
                AgentTask(
                    agent="repurpose",
                    platform=platform,
                    content=normalised.raw_content,
                    brand_context=brand_context,
                    session_id=normalised.session_id,
                    metadata={
                        **metadata,
                        # FIX Issue 11 — enforcement data passed to agent prompt
                        "banned_words": enforcement["banned_words"],
                        "required_phrases": enforcement.get("required_phrases", []),
                        "approved_openers": enforcement["approved_openers"],
                        "approved_closers": enforcement["approved_closers"],
                        "preferred_synonyms": enforcement.get("preferred_synonyms", []),
                    },
                ),
                source_platform,
            )
            for platform in platforms
        ]

        results = await asyncio.gather(*repurpose_coros, return_exceptions=True)
        pieces = []
        for platform, result in zip(platforms, results):
            if isinstance(result, Exception):
                logger.error("Repurpose failed for %s: %s", platform, result)
                pieces.append(GeneratedPiece(
                    platform=platform,
                    content="",
                    word_count=0,
                    char_count=0,
                    quality_passed=False,
                    quality_issues=[f"Repurpose error: {str(result)}"],
                    flagged_for_review=True,
                    repurposed=True,
                ))

            elif result.success:
                content_str = result.output.get("content", "")

                # ── Initial validation ────────────────────────────────────
                is_valid, issues = validate_content(
                    content=content_str,
                    platform=platform,
                    banned_words=enforcement["banned_words"],
                    # FIX Issue 12 — was required_phrases=[] silently skipping enforcement
                    required_phrases=enforcement.get("required_phrases", []),
                    approved_openers=enforcement["approved_openers"],
                    approved_closers=enforcement["approved_closers"],
                )

                # ── Retry once if hard quality gates failed ───────────────
                if not is_valid:
                    hard_issues = [i for i in issues if not i.startswith("Advisory:")]

                    # Build full enforcement context for retry — not just issue names
                    banned_list = ", ".join(enforcement["banned_words"]) if enforcement["banned_words"] else "none"
                    required_list = ", ".join(
                        p.get("text", "") for p in enforcement.get("required_phrases", [])
                        if p.get("text", "")
                    ) or "none"
                    opener_list = " | ".join(enforcement["approved_openers"][:3]) if enforcement["approved_openers"] else "none"
                    closer_list = " | ".join(enforcement["approved_closers"][:3]) if enforcement["approved_closers"] else "none"

                    retry_feedback = (
                        "REWRITE FEEDBACK — fix every issue listed below. Do not repeat these mistakes.\n\n"
                        + "\n".join(f"  ✗ {issue}" for issue in hard_issues)
                        + f"\n\nENFORCEMENT CONTEXT FOR THIS RETRY:\n"
                        + f"  Banned words (never use any of these): {banned_list}\n"
                        + f"  Required phrases (every one must appear): {required_list}\n"
                        + f"  Approved openers (pick exactly one): {opener_list}\n"
                        + f"  Approved closers (pick exactly one): {closer_list}\n"
                    )

                    retry_result = await run_repurpose_agent(
                        AgentTask(
                            agent="repurpose",
                            platform=platform,
                            content=normalised.raw_content,
                            brand_context=brand_context,
                            session_id=normalised.session_id,
                            retry_count=1,
                            metadata={
                                **metadata,
                                "banned_words": enforcement["banned_words"],
                                "required_phrases": enforcement.get("required_phrases", []),
                                "approved_openers": enforcement["approved_openers"],
                                "approved_closers": enforcement["approved_closers"],
                                "preferred_synonyms": enforcement.get("preferred_synonyms", []),
                                "retry_feedback": retry_feedback,
                            },
                        ),
                        source_platform,
                    )

                    if retry_result.success:
                        retry_content = retry_result.output.get("content", "")
                        is_valid, issues = validate_content(
                            content=retry_content,
                            platform=platform,
                            banned_words=enforcement["banned_words"],
                            required_phrases=enforcement.get("required_phrases", []),
                            approved_openers=enforcement["approved_openers"],
                            approved_closers=enforcement["approved_closers"],
                        )
                        content_str = retry_content
                        logger.info(
                            "Repurpose retry complete for %s — quality_passed: %s",
                            platform, is_valid,
                        )
                    else:
                        logger.warning(
                            "Repurpose retry failed for %s — flagging for review",
                            platform,
                        )

                # ── Hook enrichment ───────────────────────────────────────
                hooks = []
                recommended_hook_index = 0
                if metadata.get("hook_variations") and content_str:
                    try:
                        hook_task = AgentTask(
                            agent="hook",
                            platform=platform,
                            content=content_str,
                            brand_context=brand_context,
                            session_id=normalised.session_id,
                            metadata={
                                **metadata,
                                "banned_words": enforcement["banned_words"],
                                "banned_openings": [
                                    "are you tired of",
                                    "have you ever wondered",
                                    "what if you could",
                                    "in today's world",
                                    "we all know",
                                    "it's no secret",
                                ],
                            },
                        )
                        hook_result = await run_hook_agent(hook_task)
                        if hook_result.success:
                            hooks = hook_result.output.get("hooks", [])
                            recommended_hook_index = hook_result.output.get("recommended", 0)
                            content_str = apply_recommended_hook(
                                content_str, hooks, recommended_hook_index
                            )
                            logger.info(
                                "Repurpose hooks generated for %s — %d variants",
                                platform, len(hooks),
                            )
                    except Exception as e:
                        logger.warning(
                            "Hook agent failed on repurpose path for %s: %s", platform, e
                        )

                # ── SEO enrichment ────────────────────────────────────────
                seo_package = {}
                if should_run_seo(platform, metadata.get("seo_meta", False)) and content_str:
                    try:
                        seo_task = AgentTask(
                            agent="seo",
                            platform=platform,
                            content=content_str,
                            brand_context=brand_context,
                            session_id=normalised.session_id,
                            metadata=metadata,
                        )
                        seo_result = await run_seo_agent(seo_task, content_str)
                        if seo_result.success:
                            seo_package = seo_result.output
                            logger.info("Repurpose SEO generated for %s", platform)
                    except Exception as e:
                        logger.warning(
                            "SEO agent failed on repurpose path for %s: %s", platform, e
                        )

                # FIX Issue 14 — schedule fields forwarded to repurpose pieces
                pieces.append(GeneratedPiece(
                    platform=platform,
                    content=content_str,
                    word_count=len(content_str.split()),
                    char_count=len(content_str),
                    hooks=hooks,
                    seo=seo_package,
                    quality_passed=is_valid,
                    quality_issues=issues,
                    flagged_for_review=not is_valid,
                    repurposed=True,
                    publish_status=schedule_mode,
                    publish_scheduled_at=scheduled_at,
                ))

            else:
                pieces.append(GeneratedPiece(
                    platform=platform,
                    content="",
                    word_count=0,
                    char_count=0,
                    quality_passed=False,
                    quality_issues=["Repurpose agent returned no content"],
                    flagged_for_review=True,
                    repurposed=True,
                ))

    else:
        platform_coros = [
            _run_single_platform(
                platform=platform,
                normalised=normalised,
                brand_profile=brand_profile,
                metadata=metadata,
                schedule_mode=schedule_mode,
                scheduled_at=scheduled_at,
                is_repurpose=is_repurpose,
                batch_day_index=batch_day_index,
            )
            for platform in platforms
        ]

        results = await asyncio.gather(*platform_coros, return_exceptions=True)
        pieces = []
        for platform, result in zip(platforms, results):
            if isinstance(result, Exception):
                logger.error(
                    "Platform generation failed for %s session %s: %s",
                    platform, normalised.session_id, result,
                )
                pieces.append(GeneratedPiece(
                    platform=platform,
                    content="",
                    word_count=0,
                    char_count=0,
                    quality_passed=False,
                    quality_issues=[f"Generation error: {str(result)}"],
                    flagged_for_review=True,
                ))
            else:
                pieces.append(result)

    return TextPipelineResult(
        session_id=normalised.session_id,
        user_id=user_id,
        brand_id=brand_id,
        pieces=pieces,
        source_type=source_type,
        schedule_mode=schedule_mode,
        scheduled_at=scheduled_at,
        batch_mode=False,
        created_at=datetime.now(timezone.utc),
    )


async def run_batch_pipeline(
    topic_cluster: str,
    platforms: list[Platform],
    brand_id: str,
    user_id: str,
    extras,
    days: int = 7,
    detected_intent=None,
    is_repurpose: bool = False,
    source_platform: Optional[Platform] = None,
) -> list[TextPipelineResult]:
    """
    Batch mode — maps to ConfigPanel batchMode toggle.
    Generates different content angles for the same topic cluster.
    Days run sequentially to respect Groq rate limits.
    Platforms within each day still run in parallel via the graph.
    """
    from app.shared.llm import call_llm_structured

    angle_prompt = f"""
Generate {days} different content angles for the topic: "{topic_cluster}"
Each angle must approach the topic from a distinctly different perspective.
Vary the format: personal stories, data-driven, contrarian, how-to, case study.

Return valid JSON only:
{{"angles": ["angle 1 description", "angle 2 description", ...]}}
"""
    angle_result = await call_llm_structured(angle_prompt)
    angles = (
        angle_result.get("angles", [topic_cluster] * days)
        if angle_result
        else [topic_cluster] * days
    )

    results = []
    for i, angle in enumerate(angles[:days]):
        logger.info("Batch day %d/%d — angle: %s", i + 1, days, angle[:60])
        result = await run_text_pipeline(
            source_type=InputSourceType.TOPIC,
            content=angle,
            platforms=platforms,
            brand_id=brand_id,
            user_id=user_id,
            extras=extras,
            intent=detected_intent or ContentIntent.AUTO,
            batch_day_index=i,
            is_repurpose=is_repurpose,
            source_platform=source_platform,
        )
        result.batch_mode = True
        results.append(result)
    return results