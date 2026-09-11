"""
Standalone content scorer — hook quality and readability.
Used by /api/v1/text/score-hook and /api/v1/text/score-readability.

Hook scoring:
  Generates 3 alternative hooks with scroll-stopping scores (1-10)
  Provides a reason for each score
  Identifies what makes the current hook weak
  Returns recommended replacement

Readability scoring:
  Flesch Reading Ease computed without external library
  Platform-specific thresholds applied
  Grade returned: excellent / good / needs improvement
  Specific issues identified: sentences too long, syllable density etc
"""

import logging
import re
from app.pipelines.text.quality import flesch_reading_ease, is_latin_script
from app.shared.llm import call_llm_structured, GroqModel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM READABILITY THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_READABILITY_THRESHOLDS = {
    "LinkedIn":         55,
    "Twitter/X":        65,
    "Twitter/X Thread": 65,
    "Instagram":        65,
    "Facebook":         55,
    "Blog":             45,
    "Newsletter":       50,
    "YouTube":          50,
}

FLESCH_GRADES = [
    (90, "Very easy — 5th grade level"),
    (80, "Easy — 6th grade level"),
    (70, "Fairly easy — 7th grade level"),
    (60, "Standard — 8th-9th grade level"),
    (50, "Fairly difficult — 10th-12th grade level"),
    (30, "Difficult — college level"),
    (0,  "Very difficult — professional/academic"),
]


def _flesch_grade_label(score: float) -> str:
    for threshold, label in FLESCH_GRADES:
        if score >= threshold:
            return label
    return "Very difficult — professional/academic"


# ─────────────────────────────────────────────────────────────────────────────
# HOOK WEAKNESS DETECTION
# ─────────────────────────────────────────────────────────────────────────────

HOOK_WEAKNESSES = [
    ("are you tired of",       "Generic question — never starts with 'are you tired of'"),
    ("are you struggling",     "Generic struggle question — vague and self-focused"),
    ("are you finding it",     "Generic difficulty question — immediately boring"),
    ("are you ",               "Question opener — creates no tension, asks not tells"),
    ("have you ever wondered", "Generic curiosity hook — overused and vague"),
    ("in today's world",       "Filler opener — says nothing specific"),
    ("we all know",            "Assumes shared knowledge — condescending"),
    ("it's no secret",         "Filler phrase — adds no value"),
    ("i am excited to share",  "Announcement tone — nobody cares you're excited"),
    ("thrilled to announce",   "Corporate announcement — immediately boring"),
    ("as a [",                 "Identity opener — self-focused not reader-focused"),
    ("as someone who",         "Identity opener — self-focused not reader-focused"),
    ("many people",            "Vague generalisation — use a specific number instead"),
    ("many creators",          "Vague generalisation — use a specific number instead"),
    ("most people",            "Vague generalisation — use a specific number instead"),
    ("most creators",          "Vague generalisation — use a specific number instead"),
    ("did you know",           "Overused curiosity hook — feels like a quiz"),
    ("imagine ",               "Overused visualisation opener"),
    ("picture this",           "Overused scene-setting — feels like a sales script"),
]


_HOOK_WEAKNESS_NOT_EVALUATED = (
    "Pattern-based weak-hook detection not evaluated — HOOK_WEAKNESSES is an "
    "English-only phrase list and this hook is not in Latin script. Not "
    "evaluated is not the same as 'no weakness found'."
)


def _detect_hook_weakness(first_line: str) -> str | None:
    """Pattern-match the hook's opening against known weak English phrases.

    Returns ``_HOOK_WEAKNESS_NOT_EVALUATED`` — not ``None`` — for non-Latin
    script, so the caller can distinguish "checked, found nothing" from
    "never checked". Silently returning None for both would read as a
    positive result the check never actually produced (confirmed during the
    i18n investigation: for Tamil/Hindi/Korean hooks this pattern match could
    never fire either way, so a plain None was indistinguishable from "clean").
    """
    if not is_latin_script(first_line):
        return _HOOK_WEAKNESS_NOT_EVALUATED
    first_lower = first_line.strip().lower()
    for pattern, reason in HOOK_WEAKNESSES:
        if first_lower.startswith(pattern):
            return reason
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HOOK SCORER
# ─────────────────────────────────────────────────────────────────────────────

async def score_hook(
    content: str,
    platform: str,
    brand_context: str,
    banned_words: list[str] = [],
    approved_openers: list[str] = [],
    session_id: str = "",
) -> dict:
    """
    Score the hook quality of existing content and generate 3 alternatives.

    Returns:
      current_hook:         first line of the content
      current_score:        1-10 scroll-stopping score
      current_reason:       specific reason for the score
      current_weakness:     pattern-detected weakness if any (no LLM needed)
      alternatives:         3 hook variants with scores and reasons
      recommended:          index of highest-scoring alternative
      recommended_content:  full content with recommended hook applied
    """
    if not content or not content.strip():
        return {
            "error": "Content is empty",
            "current_hook": "",
            "current_score": 0,
            "current_reason": "",
            "current_weakness": None,
            "alternatives": [],
            "recommended": 0,
            "recommended_content": "",
            "platform": platform,
        }

    # Extract first meaningful line as current hook
    lines = [l.strip() for l in content.strip().split('\n') if l.strip()]
    current_hook = lines[0] if lines else content[:100]

    # Detect weakness without LLM — instant pattern match
    detected_weakness = _detect_hook_weakness(current_hook)

    # ── Banned enforcement block ──────────────────────────────────────────
    banned_enforcement = ""
    if banned_words:
        banned_list = ", ".join(f"'{w}'" for w in banned_words)
        banned_enforcement = (
            f"\nBANNED VOCABULARY — HARD RULE:\n"
            f"Never use any of these words in any hook alternative: {banned_list}\n"
            f"If you are about to write a banned word — STOP and rephrase.\n"
            f"Hooks containing banned words will be rejected automatically.\n"
        )

    # ── Approved openers reference block ─────────────────────────────────
    approved_openers_block = ""
    if approved_openers:
        approved_openers_block = (
            "\nAPPROVED OPENER EXAMPLES — the brand's proven hooks:\n"
            + "\n".join(f"  ✓ \"{o}\"" for o in approved_openers[:3])
            + "\n"
        )

    # ── Approved openers exact usage rule ────────────────────────────────
    approved_openers_exact = ""
    if approved_openers:
        approved_openers_exact = (
            "\nAPPROVED OPENER EXACT USAGE RULE:\n"
            "If an approved opener fits the content angle — use it word for word.\n"
            "Do NOT add tails to approved openers. They are complete as written.\n"
            "  ✗ 'You don't have a content problem, you have a system problem that's burning you out'\n"
            "  ✓ 'You don't have a content problem. You have a system problem.'\n"
            "The approved opener is already the sharpest version. Trust it.\n\n"
            "For Uncomfortable truth — use the brand's own story pattern:\n"
            "  Short declarative sentences. No 'most creators'. No generalisation.\n"
            "  Strong uncomfortable truth example:\n"
            "  'Three weeks of daily posting. Six weeks of silence. The guilt cycle restarts.'\n"
            "  NOT: 'Most creators are stuck in a guilt cycle...'\n"
        )

    # score_hook has no explicit language parameter — the caller (ScoreHookRequest)
    # doesn't carry one — so language is inferred from the content itself rather
    # than threaded from the API. Confirmed during the i18n investigation that
    # without this, the LLM defaulted to English alternatives even when scoring
    # non-English content, since nothing in the prompt said otherwise.
    language_note = (
        ""
        if is_latin_script(content)
        else (
            "\nLANGUAGE: The CURRENT CONTENT below is not in English. Write "
            "current_reason and all 3 alternative hooks in the SAME language "
            "as the current content — do not translate to English.\n"
        )
    )

    prompt = f"""
{language_note}
{brand_context}
{banned_enforcement}
{approved_openers_block}
{approved_openers_exact}
BANNED HOOK OPENINGS — never start any hook with these:
  ✗ "Are you tired of" / "Are you struggling" / "Are you finding it"
  ✗ "Have you ever wondered" / "Did you know"
  ✗ "Many people" / "Many creators" / "Most people" / "Most creators"
  ✗ "In today's world" / "We all know" / "It's no secret"
  ✗ "I am excited to share" / "Thrilled to announce"
  ✗ "Imagine" / "Picture this" / "As someone who"

BRAND FACTS vs AUDIENCE GOALS — critical distinction:
  ✗ NEVER use audience goal numbers as hook promises
    "land 2-3 brand deals", "grow to 10k followers" are what the
    AUDIENCE wants — not proven brand results. Using them is misleading.
  ✓ USE brand story numbers — proven and credible:
    Look in the brand context above for: specific numbers, named events,
    timeframes, real outcomes. Use them exactly as stated.

HOOK QUALITY RULES:
  ✗ Never name the product in the hook — earn attention before selling
  ✗ Never use motivational language: "transform", "revolutionize", "change your life"
  ✗ Never use vague generalisations: "many creators", "most people"
  ✓ Specific beats vague: "11 brands" beats "many brands"
  ✓ Tension beats positivity: "I either built a system or burned out" wins
  ✓ Statement beats question: tell them something, do not ask them something
  ✓ Short beats long: 2 punchy sentences beat 1 long sentence

You are scoring the hook quality of this {platform} content.

CURRENT CONTENT:
{content[:1500]}

CURRENT HOOK (first line):
"{current_hook}"

TASK:
1. Score the current hook 1-10 for scroll-stopping power
2. Give one specific reason for the score
3. Generate 3 alternative hooks

Hook 1 — Contrarian: Challenge what the audience assumes is the solution.
Hook 2 — Specific outcome: Use a real number from the brand story. Never fabricate.
Hook 3 — Uncomfortable truth: Short declarative sentences. The private observation. No preamble.

Scoring criteria:
  9-10: Specific brand fact, creates tension, reader cannot scroll past
  7-8:  Specific claim, some tension, minor weakness
  5-6:  Somewhat specific, missing urgency or tension
  3-4:  Generic or vague, could apply to any brand
  1-2:  Banned pattern, fabricated stat, or contains banned words

RECOMMENDED SELECTION RULE:
  recommended = index of the alternative with the HIGHEST score number
  scores 8, 9, 7 → recommended must be 1
  scores 7, 7, 9 → recommended must be 2
  scores 9, 8, 7 → recommended must be 0
  Never recommend a lower-scored hook over a higher-scored one.

Return valid JSON only:
{{
  "current_score": 5,
  "current_reason": "one specific reason for the score",
  "alternatives": [
    {{
      "text": "contrarian hook — approved opener used exactly if it fits, no product name",
      "style": "Contrarian",
      "score": 8,
      "reason": "specific reason this hook works"
    }},
    {{
      "text": "specific outcome hook — real brand number, not audience goal number",
      "style": "Specific outcome",
      "score": 9,
      "reason": "specific reason this hook works"
    }},
    {{
      "text": "uncomfortable truth — short declaratives, no most creators, no preamble",
      "style": "Uncomfortable truth",
      "score": 7,
      "reason": "specific reason this hook works"
    }}
  ],
  "recommended": 1
}}
"""

    # max_tokens raised for the same reason as generator.py's
    # GENERATION_MAX_TOKENS — gpt-oss-120b can exhaust the 2500 default on
    # hidden reasoning tokens alone for non-English content.
    result = await call_llm_structured(prompt, model=GroqModel.BALANCED, max_tokens=4000)
    if not result:
        logger.warning("Hook scorer LLM call failed for session %s", session_id)
        return {
            "current_hook": current_hook,
            "current_score": 0,
            "current_reason": "Scoring failed — LLM error",
            "current_weakness": detected_weakness,
            "alternatives": [],
            "recommended": 0,
            "recommended_content": content,
            "platform": platform,
        }

    # ── Apply recommended hook to full content ────────────────────────────
    
    alternatives = result.get("alternatives", [])

    if alternatives:
        max_score = max(a.get("score", 0) for a in alternatives)
        recommended_idx = next(
            i for i, a in enumerate(alternatives)
            if a.get("score", 0) == max_score
        )
    else:
        recommended_idx = 0
    recommended_content = content
    if alternatives and recommended_idx < len(alternatives):
        recommended_hook_text = alternatives[recommended_idx].get("text", "")
        if recommended_hook_text:
            recommended_content = _apply_hook_to_content(content, recommended_hook_text)

    return {
        "current_hook": current_hook,
        "current_score": result.get("current_score", 0),
        "current_reason": result.get("current_reason", ""),
        "current_weakness": detected_weakness,
        "alternatives": alternatives,
        "recommended": recommended_idx,
        "recommended_content": recommended_content,
        "platform": platform,
    }


def _apply_hook_to_content(content: str, new_hook: str) -> str:
    """Replace the first non-empty line with the new hook."""
    lines = content.strip().split('\n')
    for i, line in enumerate(lines):
        if line.strip():
            lines[i] = new_hook
            break
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# READABILITY SCORER
# ─────────────────────────────────────────────────────────────────────────────

def _count_sentences(text: str) -> int:
    sentences = re.split(r'[.!?]+', text)
    return len([s for s in sentences if s.strip()])


def _count_long_sentences(text: str, threshold: int = 25) -> int:
    sentences = re.split(r'[.!?]+', text)
    return len([s for s in sentences if len(s.split()) > threshold])


def _count_complex_words(text: str) -> int | None:
    """Count words with 3+ syllables as proxy for complexity.

    Returns None — not 0 — for non-Latin script. The ``[aeiou]+`` syllable
    proxy matches ASCII vowels only, so it silently counts 0 syllables for
    every Tamil/Devanagari/Hangul word regardless of actual complexity; a
    real 0 would misrepresent that as "no complex words found" instead of
    "not measurable this way".
    """
    if not is_latin_script(text):
        return None
    words = re.findall(r'\b\w+\b', text.lower())
    complex_count = 0
    for word in words:
        syllables = len(re.findall(r'[aeiou]+', word))
        if syllables >= 3:
            complex_count += 1
    return complex_count


def _identify_readability_issues(
    text: str,
    score: float,
    threshold: int,
    word_count: int,
    sentence_count: int,
) -> list[str]:
    """Identify specific readability issues with actionable advice."""
    issues = []

    if sentence_count > 0:
        avg_sentence_len = word_count / sentence_count
        if avg_sentence_len > 25:
            issues.append(
                f"Average sentence length is {avg_sentence_len:.0f} words — "
                f"aim for under 20 words per sentence"
            )

    long_sentences = _count_long_sentences(text)
    if long_sentences > 0:
        issues.append(
            f"{long_sentences} sentence{'s' if long_sentences > 1 else ''} "
            f"exceed 25 words — split them"
        )

    complex_words = _count_complex_words(text)
    if complex_words is not None and word_count > 0:
        complex_ratio = complex_words / word_count
        if complex_ratio > 0.15:
            issues.append(
                f"{complex_ratio:.0%} of words have 3+ syllables — "
                f"use simpler alternatives where possible"
            )

    passive_patterns = [r"\bwas \w+ed\b", r"\bwere \w+ed\b", r"\bbeen \w+ed\b"]
    passive_count = sum(
        len(re.findall(p, text, re.IGNORECASE))
        for p in passive_patterns
    )
    if passive_count > 2:
        issues.append(
            f"{passive_count} passive voice constructions found — "
            f"rewrite in active voice for more energy"
        )

    return issues


def score_readability(content: str, platform: str) -> dict:
    """
    Score readability of content for a specific platform.
    Pure computation — no LLM call.

    Returns:
      score:            Flesch Reading Ease 0-100
      threshold:        platform minimum
      grade:            excellent / good / needs improvement / poor
      grade_label:      plain English grade level
      word_count:       total words
      sentence_count:   total sentences
      avg_sentence_len: average words per sentence
      complex_word_count: words with 3+ syllables
      issues:           specific actionable issues found
      passed:           True if score >= threshold
      platform:         platform name
    """
    if not content or not content.strip():
        return {
            "score": 0,
            "threshold": PLATFORM_READABILITY_THRESHOLDS.get(platform, 45),
            "grade": "unknown",
            "grade_label": "No content provided",
            "word_count": 0,
            "sentence_count": 0,
            "avg_sentence_len": 0,
            "complex_word_count": 0,
            "issues": [],
            "passed": False,
            "platform": platform,
            "supported": True,
        }

    # Strip hashtags before scoring — they inflate syllable count
    content_clean = re.sub(r'#\w+', '', content).strip()
    threshold = PLATFORM_READABILITY_THRESHOLDS.get(platform, 45)

    if not is_latin_script(content_clean):
        # word_count/sentence_count are script-neutral (whitespace/punctuation
        # based) so still meaningful; score/grade/complex_word_count are not —
        # the Flesch formula and its syllable proxy are English-only. Report
        # honestly rather than emit a numerically-plausible but meaningless
        # score, per the i18n investigation's finding on this exact function.
        words = content_clean.split()
        sentence_count = _count_sentences(content_clean)
        return {
            "score": 0,
            "threshold": threshold,
            "grade": "unsupported",
            "grade_label": "Readability scoring not available for this language (non-Latin script)",
            "word_count": len(words),
            "sentence_count": sentence_count,
            "avg_sentence_len": round(len(words) / sentence_count, 1) if sentence_count > 0 else 0,
            "complex_word_count": 0,
            "issues": [
                "Readability scoring (Flesch Reading Ease) is calibrated for "
                "English and not available for this content's script. This is "
                "not a low score — it is not evaluated."
            ],
            "passed": True,  # never penalise for something we can't measure
            "platform": platform,
            "supported": False,
        }

    score = flesch_reading_ease(content_clean)

    words = content_clean.split()
    word_count = len(words)
    sentence_count = _count_sentences(content_clean)
    avg_sentence_len = round(word_count / sentence_count, 1) if sentence_count > 0 else 0

    if score >= threshold + 15:
        grade = "excellent"
    elif score >= threshold:
        grade = "good"
    elif score >= threshold - 10:
        grade = "needs improvement"
    else:
        grade = "poor"

    issues = _identify_readability_issues(
        content_clean, score, threshold, word_count, sentence_count
    )

    return {
        "score": round(score, 1),
        "threshold": threshold,
        "grade": grade,
        "grade_label": _flesch_grade_label(score),
        "word_count": word_count,
        "sentence_count": sentence_count,
        "avg_sentence_len": avg_sentence_len,
        "complex_word_count": _count_complex_words(content_clean),
        "issues": issues,
        "passed": score >= threshold,
        "supported": True,
        "platform": platform,
    }