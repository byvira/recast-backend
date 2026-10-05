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

from app.shared.brand_name import brand_display_name
from app.agents.text.state import TextAgentState
from app.models.text import AgentTask, GeneratedPiece, Platform
from app.pipelines.text.brand_context import build_goal_context, build_tone_and_terms, build_tone_override, build_engagement_context
from app.pipelines.text.generator import GENERIC_OPENINGS, generate_for_platform
from app.pipelines.publish.spine import to_utc_datetime
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
        "approved_vocabulary": [],
    }


async def merge_member_lexicon_enforcement(
    enforcement: dict, *, workspace_id: Optional[str], user_id: Optional[str],
) -> dict:
    """Merges a member's personal Lexicon (Remy's Vocabulary & Pronunciation
    tab) into brand-level enforcement, in place, real personal enforcement
    on top of brand enforcement, not a second parallel mechanism:
      - blacklist -> banned_words: the exact same hard-gate + prompt
        instruction brand banned_words already use (validate_content,
        banned_words.jinja).
      - whitelist -> approved_vocabulary: the positive mirror — explicitly
        tells the model these terms are cleared brand/technical vocabulary,
        not jargon to hedge around or avoid (approved_vocabulary.jinja).

    Called from every real generation entry point that builds enforcement
    from a brand profile — both the normal-generation graph's
    build_context_node and the repurpose path's _run_repurpose_path — so
    personal enforcement applies consistently regardless of which mode
    produced the content. Shared, not duplicated, since both already read
    the same MemberLexicon shape.

    No-op (brand enforcement returned unchanged, `approved_vocabulary`
    still defaults to []) when user_id/workspace_id aren't given or the
    member has no saved lexicon — never raises.
    """
    from app.db.mongo import member_lexicon

    if not user_id:
        return enforcement

    lexicon_doc = await member_lexicon.find_one(
        {"workspace_id": workspace_id, "user_id": user_id},
        {"blacklist": 1, "whitelist": 1},
    )
    if not lexicon_doc:
        return enforcement

    if lexicon_doc.get("blacklist"):
        enforcement["banned_words"] = list(
            dict.fromkeys([*enforcement["banned_words"], *lexicon_doc["blacklist"]])
        )
    if lexicon_doc.get("whitelist"):
        enforcement["approved_vocabulary"] = list(
            dict.fromkeys([*enforcement.get("approved_vocabulary", []), *lexicon_doc["whitelist"]])
        )
    return enforcement


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
    tone_override_text = build_tone_and_terms(tone.value if tone else "brand", state["language"])
    current_platform_str = str(
        state["current_platform"].value
        if hasattr(state["current_platform"], "value")
        else state["current_platform"]
    )
    engagement_context = build_engagement_context(current_platform_str)
    # What has worked for this workspace on this platform (off unless PERFORMANCE_HINT_IN_PROMPTS is on).
    from app.pipelines.text.performance_hint import best_post_hint

    hint = await best_post_hint(state["workspace_id"], current_platform_str)
    if hint:
        engagement_context = f"{engagement_context}\n\n{hint}" if engagement_context else hint

    # ── Extract all enforcement data ──────────────────────────────────────
    enforcement = _extract_enforcement_data(brand_profile)
    enforcement = await merge_member_lexicon_enforcement(
        enforcement, workspace_id=state.get("workspace_id"), user_id=state.get("user_id"),
    )

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
        brand_name = brand_display_name(brand_profile, "Brand")
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
        "engagement_context": engagement_context,
        "extras": updated_extras,
    }


async def generate_node(state: TextAgentState) -> dict:
    """
    Generates platform-specific content.
    Passes all context layers and enforcement data through task metadata.
    Writes: generated_content
    """
    if state.get("emitter"):
        await state["emitter"].pause_point()
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
        "engagement_context": state["engagement_context"],
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
    # Only the brand-rule findings the quality step does not already check itself (its banned-word, length and limit checks
    # cover the rest).
    brand_rule_prefixes = ("Generic opening", "Generic closing", "Required brand phrase")
    generation_issues = [i for i in (result.output.get("quality_issues") or []) if i.startswith(brand_rule_prefixes)]

    return {"generated_content": content, "generation_issues": generation_issues}


async def hooks_node(state: TextAgentState) -> dict:
    """
    Generates 3 hook variants if hook_variations is enabled.
    Passes full brand context so hooks stay on-brand.
    Applies recommended hook to generated_content.
    Writes: hooks, recommended_hook_index, generated_content (hook applied)
    """
    if state.get("emitter"):
        await state["emitter"].pause_point()
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
        # Was its own hand-maintained list, out of sync with generator.py's
        # GENERIC_OPENINGS — missing "the uncomfortable truth" here is how a
        # phrase already banned elsewhere kept reaching real output via this
        # node's hook generation. Now one source of truth.
        "banned_openings": GENERIC_OPENINGS,
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
        state["generated_content"], hooks, recommended_index, language=state["language"],
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
    if state.get("emitter"):
        await state["emitter"].pause_point()
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
        language=state["language"],
    )

    # The platform's own hard limits (the same rules the schedule and publish steps enforce), so a piece that cannot be posted
    # is rewritten now and not found out later. Twitter and its threads are left to their own checks above (a thread is
    # several tweets, not one).
    from app.pipelines.publish.spine import platform_key
    from app.pipelines.publish.validators import validate_for_platform

    key = platform_key(platform_str)
    if key and key != "twitter":
        try:
            _, limit_issues = validate_for_platform(key, state["generated_content"])
        except Exception:  # noqa: BLE001
            limit_issues = []
        new_limit_issues = [i for i in limit_issues if i not in quality.issues]
        if new_limit_issues:
            quality.issues = [*quality.issues, *new_limit_issues]
            quality.passed = False

    # Figures, prices and "last Thursday" stories that nothing supplied backs up. Advisory only: it never blocks or
    # rewrites, it asks the member to confirm before publishing. Compared against what the model was given.
    from app.pipelines.text.claims import unsupported_claims

    # In topic mode the "source" is a research note the model itself wrote from the topic, so a figure in it is not evidence
    # for a figure in the post. Only what the brand itself says counts there.
    from app.models.text import InputSourceType

    topic_mode = state.get("source_type") == InputSourceType.TOPIC
    claim_warnings = unsupported_claims(
        state["generated_content"],
        [state.get("brand_context") or ""] if topic_mode else
        [state.get("normalised_content") or "", state.get("raw_input") or "", state.get("brand_context") or "", state.get("content_brief") or ""],
    )
    quality.issues = [*quality.issues, *(f"Advisory: {w}" for w in claim_warnings)]

    # In topic mode nothing but the brand's own facts can back a figure or a story, so an invented one is sent back once with
    # the exact items to remove. On the rewrite it stays advisory, so a stubborn model never leaves a post flagged for this alone.
    if topic_mode and claim_warnings and state["retry_count"] == 0:
        found = []
        for warning in claim_warnings:
            quoted = warning.split('"')
            if len(quoted) >= 2 and quoted[1] not in found:
                found.append(quoted[1])
        quality.issues = [
            *quality.issues,
            "Invented detail: remove every figure, time, price, score and personal story that is not in the brand facts. "
            f"Found: {', '.join(found[:5])}.",
        ]
        quality.passed = False

    # The writing step already judged the brand's opening, closing and required-phrase rules. This gate did not, so a piece
    # that broke them passed here and was never rewritten.
    brand_rule_issues = [i for i in (state.get("generation_issues") or []) if i not in quality.issues]
    if brand_rule_issues:
        quality.issues = [*quality.issues, *brand_rule_issues]
        quality.passed = False

    # A passing draft gets one review for depth. Weak ones go back through the same rewrite step with concrete fixes.
    from app.pipelines.text.critique import review_draft, should_review
    from app.pipelines.text.generator import build_language_instruction

    if quality.passed and state["retry_count"] == 0 and should_review(state["current_platform"], state["generated_content"]):
        fixes = await review_draft(
            draft=state["generated_content"], platform=state["current_platform"], platform_label=platform_str,
            brand_context=state.get("brand_context") or "", source_content=state.get("normalised_content") or "",
            language_line=build_language_instruction(state["language"]),
        )
        if fixes:
            quality.issues = [*quality.issues, *(f"Depth: {fix}" for fix in fixes)]
            quality.passed = False

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
    if state.get("emitter"):
        await state["emitter"].pause_point()
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

    # The rewritten text has its own brand-rule findings; the old ones no longer apply to it.
    brand_rule_prefixes = ("Generic opening", "Generic closing", "Required brand phrase")
    generation_issues = [i for i in (result.output.get("quality_issues") or []) if i.startswith(brand_rule_prefixes)]

    return {
        "generated_content": rewritten_content,
        "generation_issues": generation_issues,
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
        # A planned time only records the intent. The piece stays pending and
        # is queued when somebody approves it (spine.promote_approved_intent),
        # so the worker never publishes something nobody reviewed.
        publish_status="pending",
        intended_publish_at=(
            to_utc_datetime(state["scheduled_at"]) if state["schedule_mode"] == "scheduled" else None
        ),
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
        # Nothing was written (the model returned nothing): there is no post to save, so no empty flagged piece is left behind.
        if (latest_piece.get("content") or "").strip():
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
                    input_text=state.get("raw_input"),
                    extras={
                        k: bool(state["extras"][k])
                        for k in ("hook_variations", "hashtags", "auto_cta", "seo_meta", "grammar_check", "plagiarism_check", "avoid_blacklist", "pdf_export")
                        if k in state["extras"]
                    },
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
                    intended_publish_at=latest_piece.get("intended_publish_at"),
                    extra_fields={"language": state.get("language") or None},
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
    # A post in the wrong language gets one more try, since a second pass with the reminder usually fixes it.
    if state["retry_count"] < 2 and any(i.startswith("The post is ") for i in state["quality_issues"]):
        return "retry"
    return "flag"