"""
Text pipeline orchestrator — single entry point for all text generation.
Called by API routes. Handles normal generation, repurpose mode, and batch mode.
Runs all platforms in parallel using asyncio.gather.
One auto-retry per platform on hard quality failure — handled inside the graph.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from app.agents.text.graph import build_single_platform_graph
from app.agents.text.state import build_initial_state
from app.db.mongo import brand_profiles
from app.models.text import (
    AgentTask,
    ContentGoal,
    GeneratedPiece,
    GeneratedSection,
    InputSourceType,
    NormalisedInput,
    ContentIntent,
    Platform,
    TextPipelineResult,
    ToneOverride,
)
from app.pipelines.text.brand_context import build_brand_context
from app.pipelines.text.normalizer import normalise_input
from app.pipelines.text.repurpose import run_repurpose_agent, run_structured_repurpose_agent
from app.pipelines.text.generator import GENERIC_OPENINGS, validate_content, validate_structured_sections
from app.shared.llm import call_llm_structured
from app.prompts.registry import load_prompt
from app.agents.text.nodes import _extract_enforcement_data
from app.pipelines.text.seo import run_seo_agent, should_run_seo
from app.pipelines.text.hook_agent import run_hook_agent, apply_recommended_hook
from app.agents.text.event_emitter import EventEmitter
from uuid import uuid4

logger = logging.getLogger(__name__)

# Compiled once at module load — reused for every request
_text_graph = build_single_platform_graph()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_metadata(
    extras, goal=None, tone=None, language: str = "en", default_tone: Optional[str] = None
) -> dict:
    """
    Build the metadata dict stored in TextAgentState.extras.
    Every node reads what it needs from state["extras"].
    Carries all toggle states, style overrides, and language.

    `default_tone` is the brand profile's own persistent tone default (My
    Voices > Calibration tab) — used only when the caller didn't pass an
    explicit per-run `tone` override, so a brand configured with e.g.
    "professional" gets that treatment on every generation without anyone
    needing to pick it from the ToneSelector each time. An explicit `tone`
    always wins; "brand" (or unset) means no persistent default, same as
    before this existed.

    Every real caller (text.py, text_stream.py) always constructs a
    ToneOverride object rather than passing None — defaulting to
    ToneOverride.BRAND when the user picked nothing — so "no explicit
    override" is recognised by `tone.value == "brand"`, not `tone is None`.
    """
    if tone and tone.value != "brand":
        resolved_tone = tone.value
    elif default_tone and default_tone != "brand":
        resolved_tone = default_tone
    else:
        resolved_tone = "brand"
    return {
        "hook_variations":   extras.hook_variations,
        "hashtags":          extras.hashtags,
        "auto_cta":          extras.auto_cta,
        "seo_meta":          extras.seo_meta,
        "grammar_check":     extras.grammar_check,
        "plagiarism_check":  extras.plagiarism_check,
        "avoid_blacklist":   extras.avoid_blacklist,
        "pdf_export":        extras.pdf_export,
        "goal":              goal.value if goal else None,
        "tone":              resolved_tone,
        "language":          language,
    }


def _platform_str(platform: Platform) -> str:
    """Safely convert a Platform enum or string to its string value."""
    return platform.value if hasattr(platform, "value") else str(platform)


async def _emit_failed_card(
    emitter: Optional[EventEmitter],
    platform: Platform,
    reason: str,
) -> None:
    """
    Emit output_complete with empty content so the frontend card always
    transitions out of 'queued' state, even when generation fails.
    Without this the card stays stuck on 'queued' indefinitely.
    """
    if not emitter:
        return
    try:
        await emitter.emit_output_complete(
            platform=_platform_str(platform),
            content="",
            hook_score=0,
            readability_score=0,
            readability_level="N/A",
            agent_commentary=f"⚠ Generation failed: {reason}",
            decisions=[],
            angle_used="auto",
            angle_score=0,
            hook_version=1,
            generation_time=0.0,
            piece_id="",
            hashtags=[],
            hook_alternatives=[],
        )
    except Exception as emit_err:
        logger.warning(
            "Failed to emit error card for %s: %s", platform, emit_err
        )


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE PLATFORM RUNNER
# ─────────────────────────────────────────────────────────────────────────────

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
    emitter=None,
    session_id: Optional[str] = None,
) -> GeneratedPiece:
    """
    Invokes the LangGraph for a single platform.
    Uses build_initial_state — never constructs TextAgentState manually.
    All processing happens inside graph nodes — no direct pipeline calls here.

    GUARANTEE: always emits output_complete (or a failure card) before returning.
    The frontend depends on this to transition the card out of 'queued'.
    """
    goal_value  = metadata.get("goal")
    tone_value  = metadata.get("tone", "brand")
    goal_enum   = ContentGoal(goal_value) if goal_value else None
    tone_enum   = ToneOverride(tone_value) if tone_value else ToneOverride.BRAND

    initial_state = build_initial_state(
        workspace_id=normalised.workspace_id,
        user_id=normalised.user_id,
        brand_id=normalised.brand_id,
        raw_input=normalised.raw_content,
        emitter=emitter or EventEmitter(),
        session_id=session_id or str(uuid4()),
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

    # ── Run graph (traced to LangSmith: agent:text_pipeline, ws:<id>) ─────
    try:
        from app.core.tracing import ainvoke_traced
        final_state, _trace_url = await ainvoke_traced(
            _text_graph,
            initial_state,
            run_name="text_pipeline_platform",
            agent="text_pipeline",
            workspace_id=normalised.workspace_id,
            user_id=normalised.user_id,
            extra_tags=[f"platform:{_platform_str(platform)}"],
            metadata={
                "session_id": session_id or "",
                "platform": _platform_str(platform),
                "is_repurpose": is_repurpose,
                "batch_mode": batch_mode,
                "brand_id": normalised.brand_id,
            },
        )
    except Exception as e:
        logger.error(
            "Graph invocation failed for %s session %s: %s",
            platform, session_id, e, exc_info=True,
        )
        # Always notify frontend — card must leave queued state
        await _emit_failed_card(emitter, platform, str(e))
        return GeneratedPiece(
            platform=platform,
            content="",
            word_count=0,
            char_count=0,
            quality_passed=False,
            quality_issues=[f"Graph error: {str(e)}"],
            flagged_for_review=True,
        )

    # ── Extract piece ─────────────────────────────────────────────────────
    pieces = final_state.get("pieces", [])
    if pieces:
        piece = GeneratedPiece(**pieces[-1])
        piece.repurposed = final_state.get("is_repurpose", False)
        return piece

    # Graph completed without error but returned no pieces — defensive fallback
    logger.error(
        "Graph returned no pieces for %s session %s",
        platform, session_id,
    )
    await _emit_failed_card(emitter, platform, "Graph returned no output")
    return GeneratedPiece(
        platform=platform,
        content="",
        word_count=0,
        char_count=0,
        quality_passed=False,
        quality_issues=["Graph returned no output"],
        flagged_for_review=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

async def run_text_pipeline(
    source_type: InputSourceType,
    content: str,
    platforms: list[Platform],
    brand_id: str,
    workspace_id: str,
    user_id: str,
    extras,
    emitter=None,
    session_id=None,
    goal=None,
    tone=None,
    intent=None,
    language: str = "en",
    source_platform: Optional[Platform] = None,
    is_repurpose: bool = False,
    schedule_mode: str = "now",
    scheduled_at=None,
    batch_day_index: Optional[int] = None,
    publish_targets: Optional[list[str]] = None,
    emit_completion: bool = True,
    structure_rules: Optional[list[dict]] = None,
) -> TextPipelineResult:
    """
    Main entry point for all text generation.
    Handles normal generation and repurpose mode.
    Runs all platforms in parallel via asyncio.gather.

    GUARANTEE: always emits pipeline_complete after all platforms finish,
    regardless of how many platforms failed.
    """
    brand_profile = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id})
    if not brand_profile:
        raise ValueError(f"Brand profile not found: {brand_id}")

    metadata   = _build_metadata(
        extras, goal, tone, language=language, default_tone=brand_profile.get("default_tone")
    )
    normalised = await normalise_input(
        source_type=source_type,
        content=content,
        platforms=platforms,
        workspace_id=workspace_id,
        user_id=user_id,
        brand_id=brand_id,
        language=language,
        intent=intent,
    )

    # ── Repurpose path ────────────────────────────────────────────────────
    if is_repurpose and source_platform:
        pieces = await _run_repurpose_path(
            platforms=platforms,
            normalised=normalised,
            brand_profile=brand_profile,
            metadata=metadata,
            source_platform=source_platform,
            schedule_mode=schedule_mode,
            scheduled_at=scheduled_at,
            emitter=emitter,
            session_id=session_id,
            structure_rules=structure_rules,
        )

    # ── Normal generation path ────────────────────────────────────────────
    else:
        publish_targets_set = set(publish_targets or [])
        platform_coros = [
            _run_single_platform(
                platform=platform,
                normalised=normalised,
                brand_profile=brand_profile,
                emitter=emitter,
                session_id=session_id,
                metadata=metadata,
                # publish_target marks that this specific piece is meant to
                # actually go out, not just be drafted/reviewed — only set
                # for platforms the caller explicitly picked in "Publish To".
                # Lowercased: the real publish system (token_store.get_token,
                # scheduled_posts worker) keys connections by lowercase slug
                # ("linkedin"), not the display-cased content Platform value
                # ("LinkedIn") — storing the latter here silently broke
                # every scheduled/queued piece's token lookup.
                publish_target=(
                    _platform_str(platform).lower()
                    if _platform_str(platform) in publish_targets_set
                    else None
                ),
                schedule_mode=schedule_mode,
                scheduled_at=scheduled_at,
                is_repurpose=is_repurpose,
                batch_day_index=batch_day_index,
            )
            for platform in platforms
        ]

        results = await asyncio.gather(*platform_coros, return_exceptions=True)
        pieces  = []

        for platform, result in zip(platforms, results):
            if isinstance(result, Exception):
                # gather() caught an unhandled exception — emit failure card
                # and continue so the remaining platforms still complete
                logger.error(
                    "Platform generation failed for %s session %s: %s",
                    platform, normalised.session_id, result, exc_info=True,
                )
                await _emit_failed_card(emitter, platform, str(result))
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

    # ── Emit pipeline_complete ──────────────────────────────────────────────
    # Fires after ALL platforms (success or failure) are gathered. This is
    # what closes the SSE stream on the frontend — so a batch run (several
    # of these calls sharing one emitter, one per day) must suppress every
    # call but the last, or the stream would close after day one instead of
    # after the whole batch. run_batch_pipeline passes emit_completion=False
    # for that reason and sends its own single emit_complete once every day
    # is done.
    if emitter and emit_completion:
        await emitter.emit_complete(
            session_id=session_id or normalised.session_id,
            total_pieces=len(pieces),
        )

    return TextPipelineResult(
        session_id=normalised.session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        brand_id=brand_id,
        pieces=pieces,
        source_type=source_type,
        schedule_mode=schedule_mode,
        scheduled_at=scheduled_at,
        batch_mode=False,
        batch_day_index=batch_day_index,
        source_platform=_platform_str(source_platform) if source_platform else None,
        created_at=datetime.now(timezone.utc),
        assistant_nudge=await _safe_assistant_nudge(workspace_id, user_id),
    )


async def _safe_assistant_nudge(workspace_id: str, user_id: str):
    """Cached, LLM-free voice-alignment read for the caller — attached to the
    pipeline response. Hard 150 ms cap; any slowness or error → None. This is a
    single indexed find_one, never a generation or embedding call, so it cannot
    add meaningful latency to the response.
    """
    try:
        from app.agents.personal.assist import cached_nudge
        from app.agents.personal.thresholds import NUDGE_CACHE_TIMEOUT_S

        return await asyncio.wait_for(
            cached_nudge(workspace_id, user_id), timeout=NUDGE_CACHE_TIMEOUT_S
        )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — nudge is best-effort
        return None


# ─────────────────────────────────────────────────────────────────────────────
# REPURPOSE PATH (extracted for clarity)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_repurpose_path(
    platforms: list[Platform],
    normalised: NormalisedInput,
    brand_profile: dict,
    metadata: dict,
    source_platform: Platform,
    schedule_mode: str,
    scheduled_at,
    emitter,
    session_id,
    structure_rules: Optional[list[dict]] = None,
) -> list[GeneratedPiece]:
    """
    Repurpose path — adapts existing content to new platforms.
    Each platform runs in parallel. Failed platforms emit a failure card
    and are included in the result as flagged pieces so the frontend
    always sees all requested cards.
    """
    brand_context = build_brand_context(brand_profile)
    enforcement   = _extract_enforcement_data(brand_profile)

    repurpose_coros = [
        _run_single_repurpose(
            platform=platform,
            normalised=normalised,
            brand_context=brand_context,
            enforcement=enforcement,
            metadata=metadata,
            source_platform=source_platform,
            schedule_mode=schedule_mode,
            scheduled_at=scheduled_at,
            emitter=emitter,
            session_id=session_id,
            structure_rules=structure_rules,
        )
        for platform in platforms
    ]

    results = await asyncio.gather(*repurpose_coros, return_exceptions=True)
    pieces  = []

    for platform, result in zip(platforms, results):
        if isinstance(result, Exception):
            logger.error("Repurpose failed for %s: %s", platform, result, exc_info=True)
            await _emit_failed_card(emitter, platform, str(result))
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
        else:
            pieces.append(result)

    return pieces


async def _run_single_repurpose(
    platform: Platform,
    normalised: NormalisedInput,
    brand_context: str,
    enforcement: dict,
    metadata: dict,
    source_platform: Platform,
    schedule_mode: str,
    scheduled_at,
    emitter,
    session_id,
    structure_rules: Optional[list[dict]] = None,
) -> GeneratedPiece:
    """
    Repurpose a single platform — with one retry and hook/SEO enrichment.
    Always emits output_complete before returning so the frontend card
    transitions correctly even on failure.

    When structure_rules is given (Presets' "Generate with this preset" /
    Simulate), generation and validation branch to the enforced-template
    path: run_structured_repurpose_agent() + validate_structured_sections()
    instead of the normal free-form run_repurpose_agent() + validate_content()
    — same one-retry-then-flag shape either way.
    """
    base_metadata = {
        **metadata,
        "banned_words":       enforcement["banned_words"],
        "required_phrases":   enforcement.get("required_phrases", []),
        "approved_openers":   enforcement["approved_openers"],
        "approved_closers":   enforcement["approved_closers"],
        "preferred_synonyms": enforcement.get("preferred_synonyms", []),
        "structure_rules":    structure_rules,
    }
    generate_fn = run_structured_repurpose_agent if structure_rules else run_repurpose_agent
    sections: Optional[list[GeneratedSection]] = None

    def _validate(sections_raw: Optional[list[dict]], content: str) -> tuple[bool, list[str]]:
        if structure_rules:
            return validate_structured_sections(
                sections=sections_raw or [],
                structure_rules=structure_rules,
                banned_words=enforcement["banned_words"],
                required_phrases=enforcement.get("required_phrases", []),
            )
        return validate_content(
            content=content,
            platform=platform,
            banned_words=enforcement["banned_words"],
            required_phrases=enforcement.get("required_phrases", []),
            approved_openers=enforcement["approved_openers"],
            approved_closers=enforcement["approved_closers"],
        )

    def _sections_from_output(output: dict) -> tuple[Optional[list[GeneratedSection]], str]:
        if not structure_rules:
            return None, output.get("content", "")
        raw_sections = output.get("sections", [])
        built = [
            GeneratedSection(
                section_name=s.get("section_name", rule.get("section_name", "")),
                content=s.get("content", ""),
                char_limit=rule.get("char_limit", 0),
                char_count=len(s.get("content", "")),
            )
            for s, rule in zip(raw_sections, structure_rules)
        ]
        return built, "\n\n".join(s.content for s in built)

    # ── Initial generation ────────────────────────────────────────────────
    try:
        result = await generate_fn(
            AgentTask(
                agent="repurpose",
                platform=platform,
                content=normalised.raw_content,
                brand_context=brand_context,
                session_id=normalised.session_id,
                metadata=base_metadata,
            ),
            source_platform,
        )
    except Exception as e:
        logger.error("Repurpose agent threw for %s: %s", platform, e, exc_info=True)
        await _emit_failed_card(emitter, platform, str(e))
        return GeneratedPiece(
            platform=platform, content="", word_count=0, char_count=0,
            quality_passed=False,
            quality_issues=[f"Repurpose agent error: {str(e)}"],
            flagged_for_review=True, repurposed=True,
        )

    if not result.success:
        await _emit_failed_card(emitter, platform, "Repurpose agent returned no content")
        return GeneratedPiece(
            platform=platform, content="", word_count=0, char_count=0,
            quality_passed=False,
            quality_issues=["Repurpose agent returned no content"],
            flagged_for_review=True, repurposed=True,
        )

    sections, content_str = _sections_from_output(result.output)
    raw_sections = result.output.get("sections") if structure_rules else None

    # ── Quality validation ────────────────────────────────────────────────
    is_valid, issues = _validate(raw_sections, content_str)

    # ── One retry on hard failure ─────────────────────────────────────────
    if not is_valid:
        hard_issues  = [i for i in issues if not i.startswith("Advisory:")]
        retry_feedback = _build_retry_feedback(hard_issues, enforcement)

        try:
            retry_result = await generate_fn(
                AgentTask(
                    agent="repurpose",
                    platform=platform,
                    content=normalised.raw_content,
                    brand_context=brand_context,
                    session_id=normalised.session_id,
                    retry_count=1,
                    metadata={**base_metadata, "retry_feedback": retry_feedback},
                ),
                source_platform,
            )
            if retry_result.success:
                retry_sections, retry_content = _sections_from_output(retry_result.output)
                retry_raw_sections = retry_result.output.get("sections") if structure_rules else None
                is_valid, issues = _validate(retry_raw_sections, retry_content)
                content_str = retry_content
                sections = retry_sections
                logger.info("Repurpose retry complete for %s — quality_passed: %s", platform, is_valid)
            else:
                logger.warning("Repurpose retry returned no content for %s — flagging", platform)
        except Exception as e:
            logger.warning("Repurpose retry threw for %s: %s", platform, e)

    # ── Hook enrichment ───────────────────────────────────────────────────
    hooks                 = []
    recommended_hook_index = 0

    if metadata.get("hook_variations") and content_str:
        try:
            hook_result = await run_hook_agent(
                AgentTask(
                    agent="hook",
                    platform=platform,
                    content=content_str,
                    brand_context=brand_context,
                    session_id=normalised.session_id,
                    metadata={
                        **metadata,
                        "banned_words": enforcement["banned_words"],
                        # QA-008: was a hand-copied, drifted-out-of-sync
                        # duplicate of generator.py's GENERIC_OPENINGS — the
                        # same bug class already fixed in nodes.py (see
                        # GENERIC_OPENINGS's own docstring) but missed here.
                        # This list was missing "the uncomfortable truth" and
                        # 3 others, which is exactly the phrase
                        # text/hooks/generate.jinja's "Hook 3" label steers
                        # the model toward. Import the single source of
                        # truth instead of maintaining a second copy.
                        "banned_openings": GENERIC_OPENINGS,
                    },
                )
            )
            if hook_result.success:
                hooks                  = hook_result.output.get("hooks", [])
                recommended_hook_index = hook_result.output.get("recommended", 0)
                content_str            = apply_recommended_hook(content_str, hooks, recommended_hook_index)
                logger.info("Repurpose hooks generated for %s — %d variants", platform, len(hooks))
        except Exception as e:
            logger.warning("Hook agent failed on repurpose path for %s: %s", platform, e)

    # ── SEO enrichment ────────────────────────────────────────────────────
    seo_package = {}
    if should_run_seo(platform, metadata.get("seo_meta", False)) and content_str:
        try:
            seo_result = await run_seo_agent(
                AgentTask(
                    agent="seo",
                    platform=platform,
                    content=content_str,
                    brand_context=brand_context,
                    session_id=normalised.session_id,
                    metadata=metadata,
                ),
                content_str,
            )
            if seo_result.success:
                seo_package = seo_result.output
                logger.info("Repurpose SEO generated for %s", platform)
        except Exception as e:
            logger.warning("SEO agent failed on repurpose path for %s: %s", platform, e)

    # ── Persist for real before telling the frontend this card is done ─────
    # Same fix as the normal generation path's collect_output_node — a
    # repurposed piece used to be emitted with piece_id="" too, since this
    # path never goes through the graph at all and has always built its own
    # GeneratedPiece by hand. Best-effort: never let a storage failure stop
    # the card from reaching the user.
    piece_id = ""
    try:
        from app.pipelines.text.storage import ensure_session_exists, save_live_piece

        await ensure_session_exists(
            session_id=normalised.session_id,
            workspace_id=normalised.workspace_id,
            user_id=normalised.user_id,
            brand_id=normalised.brand_id,
            source_type=(
                normalised.source_type.value
                if hasattr(normalised.source_type, "value")
                else str(normalised.source_type)
            ),
            goal=metadata.get("goal"),
            tone=metadata.get("tone"),
            is_repurpose=True,
            schedule_mode=schedule_mode,
            scheduled_at=scheduled_at,
        )
        piece_id = await save_live_piece(
            session_id=normalised.session_id,
            workspace_id=normalised.workspace_id,
            user_id=normalised.user_id,
            brand_id=normalised.brand_id,
            platform=_platform_str(platform),
            content=content_str,
            word_count=len(content_str.split()),
            char_count=len(content_str),
            hooks=hooks,
            seo=seo_package,
            quality_passed=is_valid,
            quality_issues=issues,
            flagged_for_review=not is_valid,
            repurposed=True,
            sections=[s.model_dump() for s in sections] if sections else None,
            source_platform=_platform_str(source_platform),
            # "queued"/"pending" (not the raw "now"/"scheduled" schedule_mode
            # value) is what content_pieces.publish_status and the calendar
            # query (get_calendar) actually recognise — see the identical
            # mapping in app/agents/text/nodes.py. Storing schedule_mode
            # directly left every repurposed piece with an invalid
            # publish_status ("now"), which get_calendar's $or never
            # matches, so repurposed content never appeared on the calendar.
            publish_status="queued" if schedule_mode == "scheduled" else "pending",
            publish_scheduled_at=scheduled_at,
        )
    except Exception as exc:
        logger.error(
            "Failed to persist repurposed piece for %s session %s: %s",
            platform, session_id, exc, exc_info=True,
        )

    # ── Emit output_complete — always fires ───────────────────────────────
    if emitter:
        try:
            hook_score = (
                hooks[recommended_hook_index].get("score", 0)
                if hooks and isinstance(hooks[recommended_hook_index], dict)
                else 0
            )
            await emitter.emit_output_complete(
                platform=_platform_str(platform),
                content=content_str,
                hook_score=hook_score,
                readability_score=0,
                readability_level="Standard",
                agent_commentary=(
                    "⚠ Flagged for review" if not is_valid
                    else f"Repurposed — hook {hook_score}"
                ),
                decisions=[],
                angle_used="repurpose",
                angle_score=0,
                hook_version=recommended_hook_index + 1,
                generation_time=0.0,
                piece_id=piece_id,
                hashtags=[],
                hook_alternatives=[
                    h.get("hook", "") for h in hooks
                    if isinstance(h, dict) and h.get("hook")
                ],
                language=normalised.language,
            )
        except Exception as emit_err:
            logger.warning("emit_output_complete failed for repurpose %s: %s", platform, emit_err)

    return GeneratedPiece(
        platform=platform,
        # Real piece_id from the live persist above — the caller
        # (repurpose_content in app/api/v1/text.py) used to call
        # save_pipeline_result() again on the whole result, which
        # unconditionally re-inserts a session document with the same
        # session_id ensure_session_exists() just upserted a few lines up;
        # MongoDB's unique index on content_sessions.session_id rejected
        # it, the exception was swallowed, and no piece_id ever came back.
        # That redundant call is removed now that piece_id is real here.
        piece_id=piece_id or None,
        content=content_str,
        sections=sections,
        word_count=len(content_str.split()),
        char_count=len(content_str),
        hooks=hooks,
        seo=seo_package,
        quality_passed=is_valid,
        quality_issues=issues,
        flagged_for_review=not is_valid,
        repurposed=True,
        publish_status="queued" if schedule_mode == "scheduled" else "pending",
        publish_scheduled_at=scheduled_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# BATCH PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

async def run_batch_pipeline(
    topic_cluster: str,
    platforms: list[Platform],
    brand_id: str,
    workspace_id: str,
    user_id: str,
    extras,
    days: int = 7,
    detected_intent=None,
    is_repurpose: bool = False,
    source_platform: Optional[Platform] = None,
    language: str = "en",
    emitter=None,
    outer_session_id: Optional[str] = None,
    platforms_by_day: Optional[list[list[Platform]]] = None,
    on_day_complete: Optional[Callable[[int, TextPipelineResult], Awaitable[None]]] = None,
) -> list[TextPipelineResult]:
    """
    Batch mode — maps to ConfigPanel batchMode toggle.
    Generates different content angles for the same topic cluster.
    Days run sequentially to respect Groq rate limits.
    Platforms within each day run in parallel via the graph.

    emitter/outer_session_id are only set when called from the SSE route
    (GET /pipeline/generate/stream) — the blocking /text/batch route calls
    this with neither, and every day just runs headless with no live
    progress, same as before. When an emitter is given, every day's
    run_text_pipeline call shares it so events for all N days stream over
    the one SSE connection; each day still gets its own fresh session_id
    (its own real content_sessions/content_pieces documents — one real
    generation run in its own right, just orchestrated together), and each
    day suppresses its own pipeline_complete (emit_completion=False) since
    that event closes the SSE stream — only the batch's own final
    emit_complete below, after every day is actually done, may do that.

    platforms_by_day, when given, overrides `platforms` for individual days
    (platforms_by_day[i] for day i) — lets a campaign vary which platforms
    run on which day instead of the same fixed set every day. None (every
    caller before this param existed) reproduces the original behaviour
    exactly.

    on_day_complete, when given, is awaited with (day_index, result) right
    after each day finishes — lets a caller (campaigns' generate_campaign_
    batch) persist pieces incrementally as each day completes instead of
    waiting for the whole batch, so progress can be polled mid-run. Purely
    additive: it doesn't touch the emitter/completion contract above.
    """
    if emitter:
        await emitter.emit_log(f"Planning {days} days of content angles for this topic…")

    angle_prompt = load_prompt("text/orchestrate/batch_angles", days=days, topic_cluster=topic_cluster)
    angle_result = await call_llm_structured(angle_prompt)
    angles = (
        angle_result.get("angles", [topic_cluster] * days)
        if angle_result
        else [topic_cluster] * days
    )

    results = []
    for i, angle in enumerate(angles[:days]):
        logger.info("Batch day %d/%d — angle: %s", i + 1, days, angle[:60])
        if emitter:
            await emitter.emit_log(f"Day {i + 1}/{days} — {angle[:80]}")
        day_platforms = (
            platforms_by_day[i] if platforms_by_day and i < len(platforms_by_day) else platforms
        )
        result = await run_text_pipeline(
            source_type=InputSourceType.TOPIC,
            content=angle,
            platforms=day_platforms,
            brand_id=brand_id,
            workspace_id=workspace_id,
            user_id=user_id,
            extras=extras,
            intent=detected_intent or ContentIntent.AUTO,
            batch_day_index=i,
            is_repurpose=is_repurpose,
            source_platform=source_platform,
            language=language,
            emitter=emitter,
            session_id=str(uuid4()),
            emit_completion=False,
        )
        result.batch_mode = True
        result.angle = angle
        if on_day_complete:
            await on_day_complete(i, result)
        results.append(result)

    if emitter:
        await emitter.emit_complete(
            session_id=outer_session_id or "",
            total_pieces=sum(len(r.pieces) for r in results),
        )

    return results


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_retry_feedback(hard_issues: list[str], enforcement: dict) -> str:
    """Build a structured retry prompt from quality gate failures + enforcement context.

    Renders app/prompts/fragments/retry_feedback.jinja (kind="detail").
    """
    return load_prompt(
        "fragments/retry_feedback",
        kind="detail",
        hard_issues=hard_issues,
        banned_words=enforcement["banned_words"],
        required_phrases=enforcement.get("required_phrases", []),
        approved_openers=enforcement["approved_openers"],
        approved_closers=enforcement["approved_closers"],
    )