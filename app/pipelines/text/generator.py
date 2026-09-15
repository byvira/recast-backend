print(">>> generator.py reloaded")


"""
Text content generator — single platform generation with full brand enforcement.

Context layers injected in order:
  1. brand_context       — voice, identity, audience, openers, closers
  2. tone_override_text  — tone override (empty if brand mode)
  3. goal_context        — content goal instruction
  4. content_brief       — sharpest angle from pre-analysis
  5. SPECIFICITY         — anti-generic instruction with few-shot examples
  6. ENGAGEMENT_PATTERNS — proven hook patterns
  7. approved_copy       — must-use openers, closers, required phrases
  8. retry_block         — specific rewrite feedback on retry
  9. platform_rules      — format and length requirements
 10. hashtag + cta       — extras toggles
 11. banned_instruction  — structural word replacements
 12. source content      — the actual input
"""

import logging
import re
from typing import Dict, List, Tuple

from app.models.text import AgentTask, AgentResult, Platform, LANGUAGE_NAMES
from app.prompts.registry import load_fixture, load_prompt
from app.shared.llm import GroqModel, call_llm, call_llm_structured

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# LANGUAGE — LANGUAGE_NAMES itself lives in app.models.text (the single source
# of truth shared with request validation); this module only builds the
# prompt-facing instruction text from it.
# ─────────────────────────────────────────────────────────────────────────────
def resolve_language_name(code: str) -> str:
    """Resolve a language code/string to the display name used in prompts.

    `code` is fully opaque — never validated against LANGUAGE_NAMES or any
    other fixed set, and never silently substituted with English. A code
    LANGUAGE_NAMES has a curated display name for (e.g. "ta" -> "Tamil")
    gets that nicer name; anything else is passed straight through to the
    LLM as-is ("the language identified by the code 'ml'"), which Groq/Gemini
    can resolve on their own for the vast majority of real ISO 639-1/639-3
    codes and language names without this codebase needing to know about it.
    An empty string is the one case actually treated as "no preference
    stated" (not "reject" or "assume English") — it's not a language code,
    it's the absence of one.
    """
    if not code:
        return "the caller's own language"
    normalised = code.strip()
    known = LANGUAGE_NAMES.get(normalised.lower().split("-")[0])
    if known:
        return known
    return f"the language identified by the code or name '{normalised}'"


def build_language_instruction(language_code: str) -> str:
    """One unambiguous block telling the model what language to write in.

    Deliberately does not special-case English — an explicit "write in
    English" instruction is harmless and keeps the prompt-construction path
    identical for every language, avoiding an en-only branch that could drift.

    The extra "examples below are English text used only to teach structure"
    sentence exists because live testing (2026-09-11, Hindi) showed the model
    echoing the literal English wording of the few-shot examples in
    SPECIFICITY_INSTRUCTION/ENGAGEMENT_PATTERNS as its opening line even when
    correctly instructed to write in Hindi — it was treating them as text to
    reuse, not as illustrations of structure. This line is the fix.
    """
    name = resolve_language_name(language_code)
    return load_prompt("text/generate/language_instruction", name=name)


# ─────────────────────────────────────────────────────────────────────────────
# SPECIFICITY INSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────
SPECIFICITY_INSTRUCTION = load_prompt(
    "text/generate/specificity", good_bad_pairs=load_fixture("generator_good_bad_pairs")
)


# ─────────────────────────────────────────────────────────────────────────────
# ENGAGEMENT PATTERNS
# ─────────────────────────────────────────────────────────────────────────────

ENGAGEMENT_PATTERNS = load_prompt("text/generate/engagement_patterns")


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM RULES
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_RULES = {
    Platform.LINKEDIN: load_prompt("text/generate/platform_rules/linkedin"),
    Platform.TWITTER: load_prompt(
        "text/generate/platform_rules/twitter", example_tweet=load_fixture("generator_example_tweet")
    ),
    Platform.TWITTER_THREAD: load_prompt("text/generate/platform_rules/twitter_thread"),
    Platform.INSTAGRAM: load_prompt("text/generate/platform_rules/instagram"),
    Platform.FACEBOOK: load_prompt("text/generate/platform_rules/facebook"),
    Platform.BLOG: load_prompt("text/generate/platform_rules/blog"),
    Platform.NEWSLETTER: load_prompt("text/generate/platform_rules/newsletter"),
    Platform.YOUTUBE: load_prompt("text/generate/platform_rules/youtube"),
}


# ─────────────────────────────────────────────────────────────────────────────
# HASHTAG RULES
#
# Same shape as PLATFORM_RULES above: one .jinja file per platform under
# app/prompts/text/generate/hashtag_rules/, loaded once at import time.
# Previously a plain dict of hardcoded strings, inconsistent with every other
# per-platform prompt fragment in this module — moved here to match.
# ─────────────────────────────────────────────────────────────────────────────

HASHTAG_RULES = {
    Platform.LINKEDIN: load_prompt("text/generate/hashtag_rules/linkedin"),
    Platform.INSTAGRAM: load_prompt("text/generate/hashtag_rules/instagram"),
    Platform.TWITTER: load_prompt("text/generate/hashtag_rules/twitter"),
    Platform.TWITTER_THREAD: load_prompt("text/generate/hashtag_rules/twitter_thread"),
    Platform.FACEBOOK: load_prompt("text/generate/hashtag_rules/facebook"),
    Platform.BLOG: load_prompt("text/generate/hashtag_rules/blog"),
    Platform.NEWSLETTER: load_prompt("text/generate/hashtag_rules/newsletter"),
    Platform.YOUTUBE: load_prompt("text/generate/hashtag_rules/youtube"),
}


# ─────────────────────────────────────────────────────────────────────────────
# CTA RULES
#
# Same shape as PLATFORM_RULES/HASHTAG_RULES — one .jinja file per platform
# under app/prompts/text/generate/cta_rules/.
# ─────────────────────────────────────────────────────────────────────────────

CTA_RULES = {
    Platform.LINKEDIN: load_prompt("text/generate/cta_rules/linkedin"),
    Platform.TWITTER: load_prompt("text/generate/cta_rules/twitter"),
    Platform.TWITTER_THREAD: load_prompt("text/generate/cta_rules/twitter_thread"),
    Platform.INSTAGRAM: load_prompt("text/generate/cta_rules/instagram"),
    Platform.FACEBOOK: load_prompt("text/generate/cta_rules/facebook"),
    Platform.BLOG: load_prompt("text/generate/cta_rules/blog"),
    Platform.NEWSLETTER: load_prompt("text/generate/cta_rules/newsletter"),
    Platform.YOUTUBE: load_prompt("text/generate/cta_rules/youtube"),
}


# ─────────────────────────────────────────────────────────────────────────────
# APPROVED COPY INJECTION
# ─────────────────────────────────────────────────────────────────────────────

def build_approved_copy_instruction(task: AgentTask) -> str:
    return load_prompt(
        "text/generate/approved_copy",
        openers=task.metadata.get("approved_openers", []),
        closers=task.metadata.get("approved_closers", []),
        phrases=task.metadata.get("required_phrases", []),
    )

# ─────────────────────────────────────────────────────────────────────────────
# BANNED WORDS — STRUCTURAL REPLACEMENT INSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────

def build_banned_words_instruction(
    banned_words: List[str],
    preferred_synonyms: List[Dict],
) -> str:
    """
    Build structural replacement instruction — shows what to use INSTEAD of banned words.
    Much more effective than just "don't use X" — gives the LLM an alternative.
    Skips synonyms with empty original field.

    Renders app/prompts/text/generate/banned_words.jinja.
    """
    return load_prompt(
        "text/generate/banned_words", banned_words=banned_words, preferred_synonyms=preferred_synonyms
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST-GENERATION VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
def validate_content(
    content: str,
    platform: Platform,
    banned_words: List[str],
    required_phrases: List[Dict],
    approved_openers: List[str],
    approved_closers: List[str],
) -> Tuple[bool, List[str]]:
    """
    Validate generated content against all brand rules.
    Hard violations → quality_passed = False → triggers retry in graph.
    Advisory violations → prefixed with "Advisory:" → logged but do not block.

    Returns (is_valid, list_of_issues)
    """
    hard_issues = []
    advisory_issues = []
    content_lower = content.lower()
    word_count = len(content.split())
    char_count = len(content)

    # ── Hard gate 1 — banned words (word boundary match) ─────────────────
    for word in banned_words:
        pattern = r'\b' + re.escape(word.strip().lower()) + r'\b'
        if re.search(pattern, content_lower):
            hard_issues.append(f"Banned word found: '{word}'")

    # ── Hard gate 2 — minimum length ──────────────────────────────────────
    min_words = {
        Platform.LINKEDIN: 150,
        Platform.TWITTER: 0,       # char-based
        Platform.TWITTER_THREAD: 200,
        Platform.INSTAGRAM: 100,
        Platform.FACEBOOK: 150,
        Platform.BLOG: 600,
        Platform.NEWSLETTER: 250,
        Platform.YOUTUBE: 100,
    }
    min_chars = {
        Platform.TWITTER: 200,
    }

    if platform in min_words and min_words[platform] > 0:
        if word_count < min_words[platform]:
            hard_issues.append(
                f"Content too short: {word_count} words, minimum {min_words[platform]} for {platform.value}"
            )

    if platform in min_chars:
        if char_count < min_chars[platform]:
            hard_issues.append(
                f"Content too short for Twitter: {char_count} chars (minimum 200)"
            )

    # ── Hard gate 3 — Twitter char limit ──────────────────────────────────
    if platform == Platform.TWITTER and char_count > 280:
        hard_issues.append(f"Twitter character limit exceeded: {char_count}/280")

    # ── Hard gate 4 — generic openings ────────────────────────────────────
    generic_openings = [
        "in today's world",
        "in today's fast-paced world",
        "are you tired of",
        "have you ever wondered",
        "we all know",
        "it's no secret",
        "i am excited to share",
        "thrilled to announce",
        "as someone who",
    ]
    first_200 = content_lower[:200]
    for generic in generic_openings:
        if first_200.startswith(generic) or first_200.startswith(f"\n{generic}"):
            hard_issues.append(f"Generic opening detected: '{generic}'")
            break

    # ── Hard gate 5 — generic closings ────────────────────────────────────
    generic_closings = [
        "so, are you ready to",
        "the choice is yours",
        "what are you waiting for",
        "don't hesitate to",
        "join us on this journey",
        "let's connect",
        "feel free to reach out",
    ]
    last_200 = content_lower[-200:]
    for generic in generic_closings:
        if generic in last_200:
            hard_issues.append(f"Generic closing detected: '{generic}'")
            break

    # ── Hard gate 6 — weasel words ────────────────────────────────────────
    weasel_words = [
        "many", "several", "often", "recently", "soon",
        "significant", "substantial", "various", "numerous",
    ]
    found_weasels = []
    for weasel in weasel_words:
        pattern = r'\b' + re.escape(weasel) + r'\b'
        if re.search(pattern, content_lower):
            found_weasels.append(weasel)
    if found_weasels:
        hard_issues.append(
            f"Weasel words found — replace with specific details: {', '.join(found_weasels)}"
        )

    # ── Hard gate 7 — required phrases present ────────────────────────────
    for phrase_obj in required_phrases:
        phrase = (phrase_obj.get("text") or "").strip()
        if phrase and phrase.lower() not in content_lower:
            hard_issues.append(f"Required brand phrase missing: '{phrase}'")

    # ── Advisory — approved opener used ───────────────────────────────────
    if approved_openers:
        first_line = content.strip().split('\n')[0].strip().lower()
        used_approved = any(
            opener.strip().lower()[:50] in first_line
            or first_line[:50] in opener.strip().lower()
            for opener in approved_openers
        )
        if not used_approved:
            advisory_issues.append(
                f"Advisory: Approved opener not used. First line: '{content.strip().split(chr(10))[0].strip()[:80]}'"
            )

    # ── Advisory — approved closer used ───────────────────────────────────
    if approved_closers:
        lines = [l.strip() for l in content.strip().split('\n') if l.strip()]
        # Strip hashtag lines from end before checking closer
        while lines and lines[-1].startswith('#'):
            lines.pop()
        last_line = lines[-1].lower() if lines else ""
        last_block = ' '.join(lines[-3:]).lower() if len(lines) >= 3 else last_line
        last_block = re.sub(r'#\w+', '', last_block).strip()

        used_approved = any(
            closer.strip().lower()[:50] in last_block
            or last_line[:50] in closer.strip().lower()
            for closer in approved_closers
        )
        if not used_approved:
            advisory_issues.append(
                f"Advisory: Approved closer not used. Last line: '{lines[-1][:80] if lines else ''}'"
            )

    all_issues = hard_issues + advisory_issues
    is_valid = len(hard_issues) == 0

    return is_valid, all_issues

# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
async def generate_for_platform(task: AgentTask) -> AgentResult:
    """
    Generate content for a single platform with full brand enforcement.

    Flow:
      1. Build prompt with all 12 context layers
      2. Call structured output — parse JSON
      3. Fallback to Groq plain text if structured fails
      4. Validate output against brand rules
      5. Return AgentResult with quality data attached
    """
    platform_rules = PLATFORM_RULES.get(task.platform, "")

    # ── Read pre-built context strings ────────────────────────────────────
    tone_override_text = task.metadata.get("tone_override_text", "")
    goal_context = task.metadata.get("goal_context", "")
    content_brief = task.metadata.get("content_brief", "")
    retry_feedback = task.metadata.get("retry_feedback", "")
    retry_count = task.metadata.get("retry_count", 0)

    # ── Language ───────────────────────────────────────────────────────────
    language_code = task.metadata.get("language", "en")
    language_instruction = build_language_instruction(language_code)

    # ── Brand enforcement data ────────────────────────────────────────────
    banned_words = task.metadata.get("banned_words", [])
    preferred_synonyms = task.metadata.get("preferred_synonyms", [])
    approved_openers = task.metadata.get("approved_openers", [])
    approved_closers = task.metadata.get("approved_closers", [])
    required_phrases = task.metadata.get("required_phrases", [])

    # ── Build instruction blocks ──────────────────────────────────────────
    approved_copy_instruction = build_approved_copy_instruction(task)
    banned_instruction = build_banned_words_instruction(banned_words, preferred_synonyms)

    # ── Hashtag instruction — brand vocabulary aware ──────────────────────
    if task.metadata.get("hashtags", True):
        base_hashtag_rule = HASHTAG_RULES.get(task.platform, "")
        if banned_words and base_hashtag_rule:
            banned_vocab = ", ".join(f"#{w.replace(' ', '')}" for w in banned_words)
            hashtag_instruction = (
                f"{base_hashtag_rule}\n"
                f"HASHTAG RULE: Never use these brand-banned hashtags or variations: {banned_vocab}\n"
                f"Use brand vocabulary instead — draw from: product name, core features, "
                f"brand phrases, and the specific topic of this piece."
            )
        else:
            hashtag_instruction = base_hashtag_rule
    else:
        hashtag_instruction = "Do NOT include any hashtags anywhere in the content."

    # ── CTA instruction ───────────────────────────────────────────────────
    cta_instruction = (
        CTA_RULES.get(task.platform, "")
        if task.metadata.get("auto_cta", False)
        else ""
    )

    # ── Retry block ───────────────────────────────────────────────────────
    retry_block = ""
    if retry_feedback and retry_count > 0:
        retry_block = load_prompt("fragments/retry_feedback", kind="wrapper", retry_feedback=retry_feedback)

    # ── Build full prompt ─────────────────────────────────────────────────
    prompt = load_prompt(
        "text/generate/master",
        language_instruction=language_instruction,
        brand_context=task.brand_context,
        tone_override_text=tone_override_text,
        goal_context=goal_context,
        content_brief=content_brief,
        specificity_instruction=SPECIFICITY_INSTRUCTION,
        engagement_patterns=ENGAGEMENT_PATTERNS,
        approved_copy_instruction=approved_copy_instruction,
        retry_block=retry_block,
        platform_rules=platform_rules,
        hashtag_instruction=hashtag_instruction,
        cta_instruction=cta_instruction,
        banned_instruction=banned_instruction,
        content=task.content,
        platform=task.platform.value,
    )

    # ── Call LLM — structured output ──────────────────────────────────────
    # max_tokens raised from the 2500 default: gpt-oss-120b is a reasoning
    # model that spends hidden reasoning_tokens out of the same budget as the
    # visible output. Confirmed via live testing (2026-09-11) that non-English
    # requests — Tamil in particular — can consume the entire 2500-token cap
    # on reasoning alone (reasoning_tokens=2498/2500, finish_reason=length),
    # leaving zero tokens for the actual content. 4000 leaves headroom for
    # both. See app/pipelines/text/generator.py history / Stage 1 test notes.
    #
    # call_llm_structured now defaults reasoning_effort="low" (2026-09-15) —
    # confirmed live that the unset default could burn the *entire* budget
    # on hidden reasoning for a short, plain-English prompt too, not just
    # the non-English case above; "low" fixed it (16.3s -> 5.5s on the same
    # prompt, in the full generate+hooks path, with clean output both times).
    GENERATION_MAX_TOKENS = 4000
    result = await call_llm_structured(prompt, max_tokens=GENERATION_MAX_TOKENS)

    if not result or "content" not in result:
        logger.warning(
            "Structured output failed for %s — falling back to Groq plain text",
            task.platform,
        )
        # word_count/char_count dropped from the requested schema (2026-09-15):
        # both were always overwritten by the recompute step below anyway, so
        # asking the model for them was pure downside — confirmed live that a
        # model occasionally writing e.g. "word_count": fifty instead of a
        # digit broke JSON parsing outright and forced this same fallback
        # path, for a value nothing ever used.
        fallback_prompt = prompt.replace(
            'Return valid JSON in exactly this format:\n{\n  "content": "the full generated content here",\n  "platform": "' + task.platform.value + '"\n}',
            "Output only the final content. No JSON. No explanation."
        )
        plain = await call_llm(fallback_prompt, model=GroqModel.BALANCED, max_tokens=GENERATION_MAX_TOKENS)
        content = plain.strip()
        result = {
            "content": content,
            "platform": task.platform.value,
        }

    # ── Recompute counts — never trust LLM's own count ───────────────────
    content_str = result.get("content", "")
    result["word_count"] = len(content_str.split())
    result["char_count"] = len(content_str)

    # ── Post-generation validation ────────────────────────────────────────
    is_valid, issues = validate_content(
        content=content_str,
        platform=task.platform,
        banned_words=banned_words,
        required_phrases=required_phrases,
        approved_openers=approved_openers,
        approved_closers=approved_closers,
    )

    result["quality_passed"] = is_valid
    result["quality_issues"] = issues
    result["flagged_for_review"] = not is_valid

    if not is_valid:
        hard_issues = [i for i in issues if not i.startswith("Advisory:")]
        logger.warning(
            "Validation failed for %s — hard issues: %s",
            task.platform,
            hard_issues,
        )
    else:
        advisory_issues = [i for i in issues if i.startswith("Advisory:")]
        if advisory_issues:
            logger.info(
                "Validation passed with advisories for %s: %s",
                task.platform,
                advisory_issues,
            )

    return AgentResult(
        agent="text",
        platform=task.platform,
        output=result,
        success=True,
    )
