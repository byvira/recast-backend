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
from app.agents.text.event_emitter import EventEmitter
from app.agents.text.narration import msg
import random


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_brand_context_string(brand_profile: dict , batch_day_index: Optional[int] = None) -> str:
    """
    Build the full brand context string injected into every generation prompt.
    Includes voice examples, positioning, audience, and style directives.
    Voice EXAMPLES are critical — descriptions alone are not enough.
    """
    identity = brand_profile.get("identity") or {}
    voice_tone = brand_profile.get("voice_tone") or {}
    audience = brand_profile.get("audience") or {}
    positioning = brand_profile.get("positioning_data") or {}
    manual_data = brand_profile.get("manual_data") or {}
    brand_type = brand_profile.get("brand_type", "Brand")

    # ── Identity ──────────────────────────────────────────────────────────
    if brand_type == "Product":
        name = identity.get("productName") or identity.get("name", "")
        description = identity.get("description", "")
        category = identity.get("category", "")
        use_cases = identity.get("useCases") or []
    else:
        name = identity.get("name", "")
        description = identity.get("mission") or identity.get("description", "")
        category = identity.get("industry", "")
        use_cases = []

    # ── Voice and tone ────────────────────────────────────────────────────
    tones = voice_tone.get("tones") or []
    humor = voice_tone.get("humor", "")
    emoji = voice_tone.get("emoji", "")
    style = voice_tone.get("style", "")

    lines = ["=== BRAND VOICE — FOLLOW EXACTLY ==="]
    lines.append("These instructions override all defaults. Apply every rule without exception.")
    lines.append("")

    if brand_type == "Product":
        lines.append(f"Product: {name}")
        if description:
            lines.append(f"Description: {description}")
        if category:
            lines.append(f"Category: {category}")
        if use_cases:
            lines.append("Use cases:")
            for uc in use_cases:
                lines.append(f"  - {uc}")
    else:
        lines.append(f"Personal brand: {name}")
        if description:
            lines.append(f"Mission: {description}")
        if category:
            lines.append(f"Industry: {category}")

    lines.append("")

    # ── Audience ──────────────────────────────────────────────────────────
    reading_level = audience.get("reading_level", "")
    knowledge = audience.get("knowledge_base", "")
    pain_point = audience.get("primary_pain_point", "")
    goals = audience.get("goals", "")

    if reading_level:
        lines.append(f"Audience reading level: {reading_level}")
    if knowledge:
        lines.append(f"Audience knowledge level: {knowledge}")
    if pain_point:
        lines.append(f"Audience pain point: {pain_point}")
    if goals:
        lines.append(f"Audience goals: {goals}")
    lines.append("")

    # ── Tone and style ────────────────────────────────────────────────────
    if tones:
        lines.append(f"Tone: {', '.join(tones)}")
    if humor:
        lines.append(f"Humor: {humor}")
    if emoji:
        lines.append(f"Emoji: {emoji}")
    if style:
        lines.append(f"Style directives: {style}")
    lines.append("")

    # ── Positioning (product brands) ──────────────────────────────────────
    if positioning:
        uvp = positioning.get("uvp", "")
        competitor = positioning.get("competitor", "")
        differentiator = positioning.get("differentiator", "")
        use_case = positioning.get("useCase", "")

        if uvp:
            lines.append(f"Value proposition: {uvp}")
        if competitor:
            lines.append(f"vs competitors: {competitor}")
        if differentiator:
            lines.append(f"Differentiator: {differentiator}")
        if use_case:
            lines.append(f"Real use case example: {use_case}")
        lines.append("")

    # ── Approved copy — CRITICAL ──────────────────────────────────────────
    # These are the exact voice examples the LLM must learn from
    openers = manual_data.get("openers") or []
    closers = manual_data.get("closers") or []
    phrases = manual_data.get("phrases") or []
    banned = (
        manual_data.get("banned_words")
        or manual_data.get("bannedWords")
        or []
    )
    synonyms = (
        manual_data.get("preferred_synonyms")
        or manual_data.get("preferredSynonyms")
        or []
    )

    if openers:
        if batch_day_index is not None and len(openers) > 1:
            suggested_opener = openers[batch_day_index % len(openers)]
            lines.append(f"Opener pattern for today (use this style, not word-for-word):")
            lines.append(f"  - {suggested_opener}")
        else:
            lines.append("Opener patterns (use these styles, not word-for-word):")
            for opener in openers[:3]:
                lines.append(f"  - {opener}")
        lines.append("")

    if closers:
        if batch_day_index is not None and len(closers) > 1:
           suggested_closer = closers[batch_day_index % len(closers)]
           lines.append("Closer pattern for today (use this style, not word-for-word):")
           lines.append(f"  - {suggested_closer}")
        else:
           lines.append("Closer patterns (use these styles, not word-for-word):")
           for closer in closers[:3]:
               lines.append(f"  - {closer}")
        lines.append("")

    if phrases:
        hook_phrases = [p.get("text", "") for p in phrases if p.get("placement") == "hook" and p.get("text")]
        transition_phrases = [p.get("text", "") for p in phrases if p.get("placement") == "transition" and p.get("text")]
        any_phrases = [p.get("text", "") for p in phrases if p.get("placement") == "any" and p.get("text")]
        close_phrases = [p.get("text", "") for p in phrases if p.get("placement") == "close" and p.get("text")]

        if hook_phrases:
            lines.append(f"Hook phrases: {', '.join(hook_phrases)}")
        if transition_phrases:
            lines.append(f"Transition phrases: {', '.join(transition_phrases)}")
        if any_phrases:
            lines.append(f"Signature phrases: {', '.join(any_phrases)}")
        if close_phrases:
            lines.append(f"Closing phrases: {', '.join(close_phrases)}")
        lines.append("")

    if banned:
        lines.append("BANNED WORDS — never use these:")
        for word in banned:
            lines.append(f"  ✗ {word}")
        lines.append("")

    if synonyms:
        valid_synonyms = [s for s in synonyms if s.get("original") and s.get("replacement")]
        if valid_synonyms:
            lines.append("Preferred word substitutions:")
            for syn in valid_synonyms:
                lines.append(f"  - '{syn['original']}' → '{syn['replacement']}'")
            lines.append("")

    lines.append("=== END BRAND VOICE ===")

    return "\n".join(lines)


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

    brief = await extract_content_brief(cleaned)

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

    brand_profile = await brand_profiles.find_one({"id": state["brand_id"]})
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
    tone_override_text = build_tone_override(tone.value if tone else "brand")

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

    banned_list = ", ".join(banned_words) if banned_words else "none"
    required_list = ", ".join(
        p.get("text", "") for p in required_phrases if p.get("text", "")
    ) if required_phrases else "none"
    opener_list = " | ".join(approved_openers[:3]) if approved_openers else "none"
    closer_list = " | ".join(approved_closers[:3]) if approved_closers else "none"

    retry_feedback = (
        "REWRITE FEEDBACK — fix every issue listed below. Do not repeat these mistakes.\n\n"
        + "\n".join(f"  ✗ {issue}" for issue in hard_issues)
        + f"\n\nENFORCEMENT CONTEXT FOR THIS RETRY:\n"
        + f"  Banned words (never use any of these): {banned_list}\n"
        + f"  Required phrases (every one must appear): {required_list}\n"
        + f"  Approved openers (pick exactly one): {opener_list}\n"
        + f"  Approved closers (pick exactly one): {closer_list}\n"
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
        publish_status="scheduled" if state["schedule_mode"] == "scheduled" else "pending",
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
            piece_id=latest_piece.get("piece_id", ""),
            hashtags=latest_piece.get("hashtags", []) or [],
            hook_alternatives=[],
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