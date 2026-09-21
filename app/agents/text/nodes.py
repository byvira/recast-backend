"""
Text agent graph nodes.
Each node receives the full TextAgentState and returns a dict of only
the fields it updates. No node calls another node directly.

Node responsibilities:
  normalise_node       → clean raw input, extract content brief
  build_context_node   → brand context, goal context, tone override,
                         enforcement data (banned words, openers, closers, phrases)
  generate_node        → platform-specific content generation
  hooks_node           → 3 hook variants, scoring, apply recommended
  seo_node             → SEO package for long-form platforms
  quality_check_node   → hard gates + advisory checks
  rewrite_node         → smart retry with specific feedback
  flag_node            → mark for human review
  collect_output_node  → assemble GeneratedPiece, append to pieces
  route_after_quality  → conditional routing function (not a node)
"""



import logging
from typing import Optional

from app.agents.text.state import TextAgentState
from app.models.text import AgentTask, GeneratedPiece, Platform
from app.pipelines.text.brand_context import build_goal_context, build_tone_override
from app.pipelines.text.generator import generate_for_platform
from app.pipelines.text.hook_agent import apply_recommended_hook, run_hook_agent
from app.pipelines.text.normalizer import clean_raw_content, extract_content_brief
from app.pipelines.text.quality import run_quality_gate
from app.pipelines.text.seo import run_seo_agent, should_run_seo
from app.prompts.registry import load_prompt
from app.agents.text.event_emitter import EventEmitter
from app.agents.text.narration import msg
import random


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_brand_context_string(brand_profile: dict, batch_day_index: Optional[int] = None) -> str:
    """
    Build the full brand context string injected into every generation prompt.
    Includes voice examples, positioning, audience, and style directives.
    Voice EXAMPLES are critical — descriptions alone are not enough.

    Renders app/prompts/fragments/brand_context.jinja (document_shape="agent_node").
    """
    return load_prompt(
        "fragments/brand_context",
        document_shape="agent_node",
        brand_profile=brand_profile,
        batch_day_index=batch_day_index,
    )


def _extract_enforcement_data(brand_profile: dict) -> dict:
    """
    Extract brand enforcement data from brand profile.
    Returns dict of approved_openers, approved_closers, required_phrases,
    banned_words, preferred_synonyms.
    Handles both camelCase (frontend) and snake_case (backend model) field names.
    """
    manual_data = brand_profile.get("manual_data") or {}

    banned_words = (
        manual_data.get("banned_words")
        or manual_data.get("bannedWords")
        or []
    )
    preferred_synonyms = (
        manual_data.get("preferred_synonyms")
        or manual_data.get("preferredSynonyms")
        or []
    )
    openers = manual_data.get("openers") or []
    closers = manual_data.get("closers") or []
    phrases = manual_data.get("phrases") or []

    return {
        "banned_words": banned_words,
        "preferred_synonyms": preferred_synonyms,
        "approved_openers": openers,
        "approved_closers": closers,
        "required_phrases": phrases,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NODES
# ─────────────────────────────────────────────────────────────────────────────

async def normalise_node(state: TextAgentState) -> dict:
    """
    Cleans raw_input and extracts the content brief.
    raw_input is already scraped/researched by orchestrator.
    This node does the final clean pass and pre-analysis only.
    Writes: normalised_content, content_brief
    """
    emitter: EventEmitter = state.get("emitter")

    cleaned = await clean_raw_content(state["raw_input"])
    word_count = len(cleaned.split())

    if emitter:
        await emitter.emit_log(msg.analyzing_source(word_count=word_count))
        source_type = str(
            state["source_type"].value
            if hasattr(state.get("source_type"), "value")
            else state.get("source_type", "text")
        )
        await emitter.emit_log(msg.source_type_detected(source_type))

    brief = await extract_content_brief(cleaned, language=state["language"])

    return {
        "normalised_content": cleaned,
        "content_brief": brief,
    }


async def build_context_node(state: TextAgentState) -> dict:
    """
    Builds all prompt context strings and enforcement data from brand profile.
    Fetches brand profile once — all downstream nodes read from state.

    What this node builds:
      brand_context      → full voice string injected into every prompt
      goal_context       → goal instruction string
      tone_override_text → tone override instruction string
      extras             → updated with all enforcement data:
                           banned_words, approved_openers, approved_closers,
                           required_phrases, preferred_synonyms

    Handles both camelCase (frontend save) and snake_case (backend model).
    """
    from app.db.mongo import brand_profiles

    _brand_query = {"id": state["brand_id"]}
    if state.get("workspace_id"):
        _brand_query["workspace_id"] = state["workspace_id"]
    brand_profile = await brand_profiles.find_one(_brand_query)
    if not brand_profile:
        logger.error("Brand profile not found: %s", state["brand_id"])
        raise ValueError(f"Brand profile not found: {state['brand_id']}")

    # ── Build brand context string ────────────────────────────────────────
    brand_context = _build_brand_context_string(
    brand_profile,
    batch_day_index=state.get("batch_day_index"),
)

    # ── Build goal and tone context ───────────────────────────────────────
    goal = state["goal"]
    tone = state["tone"]
    goal_context = build_goal_context(goal.value if goal else None)
    tone_override_text = build_tone_override(tone.value if tone else "brand", state["language"])

    # ── Extract all enforcement data ──────────────────────────────────────
    enforcement = _extract_enforcement_data(brand_profile)

    # ── Debug log — confirms banned words are being read ──────────────────
    logger.info(
        "Brand %s — banned_words: %s | openers: %d | closers: %d | phrases: %d",
        state["brand_id"][:8],
        enforcement["banned_words"],
        len(enforcement["approved_openers"]),
        len(enforcement["approved_closers"]),
        len(enforcement["required_phrases"]),
    )

    # ── Merge enforcement data into extras ────────────────────────────────
    # Preserves all existing extras toggle states from the request
    updated_extras = {
        **state["extras"],
        **enforcement,
    }

    # ── Emit brand voice loaded ───────────────────────────────────────────
    emitter: EventEmitter = state.get("emitter")
    if emitter:
        identity = brand_profile.get("identity") or {}
        brand_name = (
            identity.get("productName")
            or identity.get("name")
            or "Brand"
        )
        rules_count = (
            len(enforcement["banned_words"])
            + len(enforcement["approved_openers"])
            + len(enforcement["approved_closers"])
            + len(enforcement["required_phrases"])
        )
        await emitter.emit_log(msg.brand_voice_loaded(rules_count=rules_count, brand_name=brand_name))
        await emitter.emit_log(msg.banned_words_loaded(count=len(enforcement["banned_words"])))

    return {
        "brand_context": brand_context,
        "goal_context": goal_context,
        "tone_override_text": tone_override_text,
        "extras": updated_extras,
    }


async def generate_node(state: TextAgentState) -> dict:
    """
    Generates platform-specific content.
    Passes all context layers and enforcement data through task metadata.
    Writes: generated_content
    """
    emitter: EventEmitter = state.get("emitter")
    platform_str = str(
        state["current_platform"].value
        if hasattr(state["current_platform"], "value")
        else state["current_platform"]
    )

    if emitter:
        await emitter.emit_log(msg.generating_platform(platform=platform_str, angle="Auto"))
        await emitter.emit_log(msg.platform_formatting(platform=platform_str))

    all_phrases = state["extras"].get("required_phrases", [])
    selected_phrases = random.sample(all_phrases, min(2, len(all_phrases)))


    task_metadata = {
        **state["extras"],
        "tone_override_text": state["tone_override_text"],
          "required_phrases": selected_phrases,
        "goal_context": state["goal_context"],
        "content_brief": state["content_brief"],
        "retry_feedback": state["retry_feedback"],
        "retry_count": state["retry_count"],
        # Explicit — do not rely on state["extras"] happening to carry this.
        # generate_for_platform() reads task.metadata["language"] to build the
        # prompt's language instruction (app/pipelines/text/generator.py).
        "language": state["language"],
    }

    task = AgentTask(
        agent="text",
        platform=state["current_platform"],
        content=state["normalised_content"],
        brand_context=state["brand_context"],
        session_id=state["session_id"],
        retry_count=state["retry_count"],
        metadata=task_metadata,
    )

    result = await generate_for_platform(task)
    content = result.output.get("content", "")

    return {"generated_content": content}


async def hooks_node(state: TextAgentState) -> dict:
    """
    Generates 3 hook variants if hook_variations is enabled.
    Passes full brand context so hooks stay on-brand.
    Applies recommended hook to generated_content.
    Writes: hooks, recommended_hook_index, generated_content (hook applied)
    """
    emitter: EventEmitter = state.get("emitter")
    platform_str = str(
        state["current_platform"].value
        if hasattr(state["current_platform"], "value")
        else state["current_platform"]
    )

    if not state["extras"].get("hook_variations", True):
        return {
            "hooks": [],
            "recommended_hook_index": 0,
        }

    if emitter:
        await emitter.emit_log(msg.generating_hooks(platform=platform_str))

    task = AgentTask(
    agent="hook",
    platform=state["current_platform"],
    content=state["generated_content"],
    brand_context=state["brand_context"],
    session_id=state["session_id"],
    metadata={
        **state["extras"],
        "tone_override_text": state["tone_override_text"],
        "goal_context": state["goal_context"],
        "banned_openings": [
            "are you tired of",
            "have you ever wondered",
            "what if you could",
            "in today's world",
            "we all know",
            "it's no secret",
            "i am excited to share",
            "as someone who",
            "as a [profession]",
        ],
    },
)
    
    hook_result = await run_hook_agent(task)

    if not hook_result.success or not hook_result.output.get("hooks"):
        logger.warning(
            "Hook agent returned no hooks for %s session %s",
            state["current_platform"], state["session_id"],
        )
        return {
            "hooks": [],
            "recommended_hook_index": 0,
        }

    hooks = hook_result.output.get("hooks", [])
    recommended_index = hook_result.output.get("recommended", 0)

    if emitter:
        # Score each hook variant in the log
        for i, hook in enumerate(hooks):
            hook_score = hook.get("score", 0) if isinstance(hook, dict) else 0
            await emitter.emit_log(msg.hook_scored(version=i + 1, score=hook_score, threshold=75))
        await emitter.emit_log(msg.hook_selected(version=recommended_index + 1, score=hooks[recommended_index].get("score", 0) if hooks and isinstance(hooks[recommended_index], dict) else 0))

    content_with_hook = apply_recommended_hook(
        state["generated_content"], hooks, recommended_index
    )

    return {
        "hooks": hooks,
        "recommended_hook_index": recommended_index,
        "generated_content": content_with_hook,
    }


async def seo_node(state: TextAgentState) -> dict:
    """
    Generates SEO package for Blog, Newsletter, YouTube when seo_meta enabled.
    Skipped for all other platforms or when seo_meta is False.
    Writes: seo_package
    """
    emitter: EventEmitter = state.get("emitter")
    platform = state["current_platform"]
    seo_meta = state["extras"].get("seo_meta", False)

    if not should_run_seo(platform, seo_meta):
        return {"seo_package": {}}

    platform_str = str(platform.value if hasattr(platform, "value") else platform)
    if emitter:
        await emitter.emit_log(f"Generating SEO package for {platform_str}...")

    task = AgentTask(
        agent="seo",
        platform=platform,
        content=state["generated_content"],
        brand_context=state["brand_context"],
        session_id=state["session_id"],
        metadata=state["extras"],
    )

    seo_result = await run_seo_agent(task, state["generated_content"])

    if not seo_result.success:
        logger.warning(
            "SEO agent failed for %s session %s",
            platform, state["session_id"],
        )
        return {"seo_package": {}}

    return {"seo_package": seo_result.output}


async def quality_check_node(state: TextAgentState) -> dict:
    """
    Runs all quality gates against generated_content.
    Hard gates: banned words, minimum length, Twitter char limit.
    Advisory: readability, CTA presence, grammar.
    Banned words come from extras — build_context_node stored them there.
    Writes: quality_passed, quality_issues, readability_score
    """
    emitter: EventEmitter = state.get("emitter")
    banned_words = state["extras"].get("banned_words", [])
    platform_str = str(
        state["current_platform"].value
        if hasattr(state["current_platform"], "value")
        else state["current_platform"]
    )

    if emitter:
        await emitter.emit_log(msg.scoring_readability(platform=platform_str))

    # ── Debug log ─────────────────────────────────────────────────────────
    logger.info(
        "Quality gate — platform: %s | banned_words: %s | content length: %d",
        state["current_platform"],
        banned_words,
        len(state["generated_content"]),
    )

    quality = await run_quality_gate(
        content=state["generated_content"],
        platform=state["current_platform"],
        brand_context=state["brand_context"],
        banned_words=banned_words,
        avoid_blacklist=state["extras"].get("avoid_blacklist", True),
        grammar_check=state["extras"].get("grammar_check", False),
    )

    if emitter:
        readability_level = getattr(quality, "readability_level", "Standard") or "Standard"
        hook_score = state.get("hooks", [{}])[0].get("score", 0) if state.get("hooks") and isinstance(state["hooks"][0], dict) else 0
        await emitter.emit_log(msg.scores_complete(
            platform=platform_str,
            hook=hook_score,
            readability=readability_level,
        ))
        if not quality.passed:
            hard_issues = [i for i in quality.issues if not i.startswith("Advisory:")]
            for issue in hard_issues:
                await emitter.emit_log(f"Quality gate failed: {issue}")

    return {
        "quality_passed": quality.passed,
        "quality_issues": quality.issues,
        "readability_score": quality.readability_score,
    }


async def rewrite_node(state: TextAgentState) -> dict:
    """
    Builds specific rewrite feedback from quality_issues.
    Increments retry_count.
    Re-runs generation with feedback injected.
    Only hard issues included in feedback — advisory excluded.
    Enforcement context (banned words, required phrases, openers, closers)
    included in retry feedback so LLM knows exactly what to fix and use instead.
    Writes: generated_content, retry_count, extras (updated with retry_feedback)
    """
    hard_issues = [
        issue for issue in state["quality_issues"]
        if not issue.startswith("Advisory:")
    ]

    # ── Build enforcement context for retry ───────────────────────────────
    banned_words = state["extras"].get("banned_words", [])
    required_phrases = state["extras"].get("required_phrases", [])
    approved_openers = state["extras"].get("approved_openers", [])
    approved_closers = state["extras"].get("approved_closers", [])

    # Renders app/prompts/fragments/retry_feedback.jinja (kind="detail") — same
    # template used by orchestrator.py::_build_retry_feedback. Note: this now
    # renders "none" (not blank) when required_phrases is non-empty but every
    # entry lacks usable text, matching orchestrator.py's behavior — the two
    # implementations disagreed on this edge case before consolidation.
    retry_feedback = load_prompt(
        "fragments/retry_feedback",
        kind="detail",
        hard_issues=hard_issues,
        banned_words=banned_words,
        required_phrases=required_phrases,
        approved_openers=approved_openers,
        approved_closers=approved_closers,
    )

    updated_extras = {**state["extras"], "retry_feedback": retry_feedback}

    task_metadata = {
        **updated_extras,
        "tone_override_text": state["tone_override_text"],
        "goal_context": state["goal_context"],
        "content_brief": state["content_brief"],
        "retry_feedback": retry_feedback,
        "retry_count": state["retry_count"] + 1,
    }

    task = AgentTask(
        agent="text",
        platform=state["current_platform"],
        content=state["normalised_content"],
        brand_context=state["brand_context"],
        session_id=state["session_id"],
        retry_count=state["retry_count"] + 1,
        metadata=task_metadata,
    )

    result = await generate_for_platform(task)
    rewritten_content = result.output.get("content", "")

    logger.info(
        "Rewrite complete for %s — retry %d",
        state["current_platform"],
        state["retry_count"] + 1,
    )

    emitter: EventEmitter = state.get("emitter")
    platform_str = str(
        state["current_platform"].value
        if hasattr(state["current_platform"], "value")
        else state["current_platform"]
    )
    if emitter:
        await emitter.emit_log(msg.retrying(platform=platform_str, attempt=state["retry_count"] + 1))

    return {
        "generated_content": rewritten_content,
        "retry_count": state["retry_count"] + 1,
        "extras": updated_extras,
    }

async def flag_node(state: TextAgentState) -> dict:
    """
    Sets flagged_for_review to True.
    Content is not modified — returned as-is with the flag.
    Frontend shows a review indicator on flagged pieces.
    Writes: flagged_for_review
    """
    emitter: EventEmitter = state.get("emitter")
    platform_str = str(
        state["current_platform"].value
        if hasattr(state["current_platform"], "value")
        else state["current_platform"]
    )

    logger.warning(
        "Content flagged for review — %s session %s — issues: %s",
        state["current_platform"],
        state["session_id"],
        state["quality_issues"],
    )

    if emitter:
        hard_issues = [i for i in state["quality_issues"] if not i.startswith("Advisory:")]
        reason = hard_issues[0] if hard_issues else "quality gate failed after retry"
        await emitter.emit_log(msg.platform_failed(platform=platform_str, reason=reason))
        await emitter.emit_log(f"⚠ {platform_str} flagged for human review — content saved as draft")

    return {"flagged_for_review": True}


async def collect_output_node(state: TextAgentState) -> dict:
    """
    Assembles the final GeneratedPiece from all state fields.
    Serialises to dict — TypedDict cannot hold Pydantic models directly.
    Appends to pieces list.
    Emits output_complete via SSE emitter — fires even when flagged,
    so the frontend card always transitions out of the queued state.
    Writes: pieces (appended)
    """
    content = state["generated_content"]

    piece = GeneratedPiece(
        platform=state["current_platform"],
        content=content,
        word_count=len(content.split()),
        char_count=len(content),
        hooks=state["hooks"],
        seo=state["seo_package"],
        readability_score=state.get("readability_score"),
        quality_passed=state["quality_passed"],
        quality_issues=state["quality_issues"],
        flagged_for_review=state["flagged_for_review"],
        publish_target=state["publish_target"],
        # "queued" (not the old, worker-incompatible "scheduled") is what
        # app/workers/scheduled_posts.py polls for — see PublishStatus.
        publish_status="queued" if state["schedule_mode"] == "scheduled" else "pending",
        publish_scheduled_at=state["scheduled_at"],
    )

    pieces = state["pieces"] + [piece.model_dump()]

    # ── Emit output_complete so the SSE panel transitions the card ────────
    # Fires regardless of flagged_for_review — frontend needs the signal
    # either way to move the card from queued → awaiting_approval.
    emitter: EventEmitter = state.get("emitter")
    platform = str(
        state["current_platform"].value
        if hasattr(state["current_platform"], "value")
        else state["current_platform"]
    )

    if emitter:
        latest_piece = pieces[-1]
        hook_score = latest_piece.get("hook_score", 0) or 0
        is_flagged = latest_piece.get("flagged_for_review", False)

        # ── Persist for real before telling the frontend this card is done ──
        # Every SSE-generated piece used to be emitted with piece_id="" —
        # nothing was ever saved, so approve/refine/rescore/versions had no
        # real piece to act on no matter what the UI did. Best-effort: a
        # storage failure here must never stop the card from reaching the
        # user, so it falls back to the old empty-id behaviour and logs
        # rather than raising.
        piece_id = ""
        try:
            from app.pipelines.text.storage import ensure_session_exists, save_live_piece

            source_type = state.get("source_type")
            await ensure_session_exists(
                session_id=state["session_id"],
                workspace_id=state["workspace_id"],
                user_id=state["user_id"],
                brand_id=state["brand_id"],
                source_type=source_type.value if hasattr(source_type, "value") else str(source_type),
                goal=state["extras"].get("goal"),
                tone=state["extras"].get("tone"),
                is_repurpose=state.get("is_repurpose", False),
                batch_mode=state.get("batch_mode", False),
                schedule_mode=state.get("schedule_mode", "now"),
                scheduled_at=state.get("scheduled_at"),
            )
            piece_id = await save_live_piece(
                session_id=state["session_id"],
                workspace_id=state["workspace_id"],
                user_id=state["user_id"],
                brand_id=state["brand_id"],
                platform=platform,
                content=latest_piece.get("content", ""),
                word_count=latest_piece.get("word_count", 0),
                char_count=latest_piece.get("char_count", 0),
                hooks=latest_piece.get("hooks", []),
                seo=latest_piece.get("seo", {}),
                quality_passed=latest_piece.get("quality_passed", True),
                quality_issues=latest_piece.get("quality_issues", []),
                flagged_for_review=is_flagged,
                readability_score=latest_piece.get("readability_score"),
                repurposed=latest_piece.get("repurposed", False),
                publish_status=latest_piece.get("publish_status"),
                publish_scheduled_at=latest_piece.get("publish_scheduled_at"),
                publish_target=latest_piece.get("publish_target"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "collect_output_node: failed to persist piece for %s session %s: %s",
                platform, state.get("session_id"), exc, exc_info=True,
            )

        # Real piece_id onto the piece this node is about to return — without
        # this, the blocking (non-SSE) caller's own redundant save_pipeline_
        # result() call was the only source of a piece_id, and that call
        # unconditionally re-inserts a session document with the same
        # session_id this block just (idempotently) upserted above, which
        # MongoDB's unique index rejects. See app/api/v1/text.py — the
        # redundant call is removed there now that piece_id is real here.
        if piece_id:
            pieces[-1]["piece_id"] = piece_id

        commentary = msg.build_card_commentary(
            angle_name="Auto",
            angle_score=0,
            hook_version=1,
            hook_score=hook_score,
            threshold=75,
        )
        if is_flagged:
            commentary += " · ⚠ Flagged for review"

        await emitter.emit_output_complete(
            platform=platform,
            content=latest_piece.get("content", ""),
            hook_score=hook_score,
            readability_score=latest_piece.get("readability_score", 0) or 0,
            readability_level=latest_piece.get("readability_level", "Standard") or "Standard",
            agent_commentary=commentary,
            decisions=[],
            angle_used="auto",
            angle_score=0,
            hook_version=1,
            generation_time=0.0,
            piece_id=piece_id,
            hashtags=latest_piece.get("hashtags", []) or [],
            hook_alternatives=[],
            language=state.get("language", "en"),
            batch_day_index=state.get("batch_day_index"),
        )
        await emitter.emit_log(msg.platform_complete(platform=platform, hook_score=hook_score))

    return {"pieces": pieces}


def route_after_quality(state: TextAgentState) -> str:
    """
    Conditional routing after quality_check_node.
    passed → collect_output
    retry  → rewrite_node (first failure only)
    flag   → flag_node (after retry also fails)
    """
    if state["quality_passed"]:
        return "passed"
    if state["retry_count"] < 1:
        return "retry"
    return "flag"