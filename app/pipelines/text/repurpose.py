import logging
from app.models.text import AgentTask, AgentResult, Platform
from app.pipelines.text.brand_context import build_goal_context, build_tone_override
from app.pipelines.text.generator import (
    PLATFORM_RULES,
    build_approved_copy_instruction,
    build_banned_words_instruction,
    build_language_instruction,
)
from app.prompts.registry import load_prompt
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)


# Maps a (source, target) Platform pair to the key its instruction text is
# rendered under in app/prompts/text/repurpose/platform_pairs.jinja. A pair
# absent here falls back to app/prompts/text/repurpose/fallback.jinja.
_REPURPOSE_PAIR_KEYS = {
    (Platform.BLOG, Platform.LINKEDIN): "blog_linkedin",
    (Platform.BLOG, Platform.TWITTER_THREAD): "blog_twitter_thread",
    (Platform.BLOG, Platform.NEWSLETTER): "blog_newsletter",
    (Platform.BLOG, Platform.INSTAGRAM): "blog_instagram",
    (Platform.LINKEDIN, Platform.TWITTER): "linkedin_twitter",
    (Platform.LINKEDIN, Platform.TWITTER_THREAD): "linkedin_twitter_thread",
    (Platform.LINKEDIN, Platform.INSTAGRAM): "linkedin_instagram",
    (Platform.LINKEDIN, Platform.BLOG): "linkedin_blog",
    (Platform.NEWSLETTER, Platform.LINKEDIN): "newsletter_linkedin",
    (Platform.NEWSLETTER, Platform.TWITTER_THREAD): "newsletter_twitter_thread",
    (Platform.TWITTER_THREAD, Platform.BLOG): "twitter_thread_blog",
    (Platform.TWITTER_THREAD, Platform.LINKEDIN): "twitter_thread_linkedin",
}


async def run_repurpose_agent(task: AgentTask, source_platform: Platform) -> AgentResult:
    pair_key = _REPURPOSE_PAIR_KEYS.get((source_platform, task.platform))
    if pair_key:
        instruction = load_prompt("text/repurpose/platform_pairs", pair=pair_key)
    else:
        instruction = load_prompt("text/repurpose/fallback", target=task.platform.value)

    goal_context = build_goal_context(task.metadata.get("goal"))
    tone_override = build_tone_override(task.metadata.get("tone"), task.metadata.get("language", "en"))
    platform_rules = PLATFORM_RULES.get(task.platform, "")
    language_instruction = build_language_instruction(task.metadata.get("language", "en"))

    # ── Enforcement data from metadata ────────────────────────────────────
    banned_words = task.metadata.get("banned_words", [])
    required_phrases = task.metadata.get("required_phrases", [])
    approved_openers = task.metadata.get("approved_openers", [])
    approved_closers = task.metadata.get("approved_closers", [])
    preferred_synonyms = task.metadata.get("preferred_synonyms", [])

    # ── Build enforcement instruction blocks ──────────────────────────────
    approved_copy_instruction = build_approved_copy_instruction(task)
    banned_instruction = build_banned_words_instruction(banned_words, preferred_synonyms)

    # ── Retry feedback block ──────────────────────────────────────────────
    retry_feedback = task.metadata.get("retry_feedback", "")
    retry_count = task.retry_count or 0
    retry_block = ""
    if retry_feedback and retry_count > 0:
        retry_block = load_prompt("fragments/retry_feedback", kind="wrapper", retry_feedback=retry_feedback)

    # ── Build prompt — always built regardless of retry state ────────────
    prompt = load_prompt(
        "text/repurpose/master",
        language_instruction=language_instruction,
        instruction=instruction,
        retry_block=retry_block,
        source_platform=source_platform.value,
        content=task.content,
        platform_rules=platform_rules,
        approved_copy_instruction=approved_copy_instruction,
        goal_context=goal_context,
        tone_override=tone_override,
        banned_instruction=banned_instruction,
        brand_context=task.brand_context,
        platform=task.platform.value,
    )

    # See app/pipelines/text/generator.py's GENERATION_MAX_TOKENS comment —
    # gpt-oss-120b can burn its entire token budget on hidden reasoning for
    # non-English requests, leaving no room for visible output at the 2500 default.
    result = await call_llm_structured(prompt, max_tokens=4000)

    if not result or "content" not in result:
        return AgentResult(
            agent="repurpose", platform=task.platform, output={}, success=False
        )

    content_str = result.get("content", "")
    result["word_count"] = len(content_str.split())
    result["char_count"] = len(content_str)

    return AgentResult(
        agent="repurpose", platform=task.platform, output=result, success=True
    )


async def run_structured_repurpose_agent(task: AgentTask, source_platform: Platform) -> AgentResult:
    """Enforced-template counterpart to run_repurpose_agent() — generates
    task.metadata["structure_rules"] (a list of {section_name, char_limit,
    guidelines} dicts) as named, independently-length-limited sections
    instead of one free-form content string. See app/prompts/text/repurpose/
    structured.jinja and app.pipelines.text.generator.validate_structured_sections
    for the enforcement half of this feature."""
    structure_rules = task.metadata.get("structure_rules") or []

    pair_key = _REPURPOSE_PAIR_KEYS.get((source_platform, task.platform))
    if pair_key:
        instruction = load_prompt("text/repurpose/platform_pairs", pair=pair_key)
    else:
        instruction = load_prompt("text/repurpose/fallback", target=task.platform.value)

    goal_context = build_goal_context(task.metadata.get("goal"))
    tone_override = build_tone_override(task.metadata.get("tone"), task.metadata.get("language", "en"))
    platform_rules = PLATFORM_RULES.get(task.platform, "")
    language_instruction = build_language_instruction(task.metadata.get("language", "en"))

    banned_words = task.metadata.get("banned_words", [])
    preferred_synonyms = task.metadata.get("preferred_synonyms", [])
    approved_copy_instruction = build_approved_copy_instruction(task)
    banned_instruction = build_banned_words_instruction(banned_words, preferred_synonyms)

    retry_feedback = task.metadata.get("retry_feedback", "")
    retry_count = task.retry_count or 0
    retry_block = ""
    if retry_feedback and retry_count > 0:
        retry_block = load_prompt("fragments/retry_feedback", kind="wrapper", retry_feedback=retry_feedback)

    prompt = load_prompt(
        "text/repurpose/structured",
        language_instruction=language_instruction,
        instruction=instruction,
        retry_block=retry_block,
        source_platform=source_platform.value,
        content=task.content,
        platform_rules=platform_rules,
        approved_copy_instruction=approved_copy_instruction,
        goal_context=goal_context,
        tone_override=tone_override,
        banned_instruction=banned_instruction,
        brand_context=task.brand_context,
        platform=task.platform.value,
        structure_rules=structure_rules,
    )

    result = await call_llm_structured(prompt, max_tokens=4000)

    if not result or "sections" not in result or not isinstance(result["sections"], list):
        return AgentResult(
            agent="repurpose", platform=task.platform, output={}, success=False
        )

    return AgentResult(
        agent="repurpose", platform=task.platform, output=result, success=True
    )