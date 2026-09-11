"""
Quality gates for generated content.
Hard gates block content and trigger auto-retry.
Advisory checks flag for review but never block.

Maps to:
  extras.avoidBlacklist → banned_words check (hard gate)
  extras.grammarCheck   → passive voice and weasel word check (advisory)
"""

import logging
import re
from typing import Optional
from app.models.text import Platform, QualityResult
from app.shared.llm import GroqModel, call_llm

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SCRIPT DETECTION
#
# flesch_reading_ease() below (and scorer.py's hook-weakness pattern matching,
# and style.py's fingerprint()) are all calibrated for English: ASCII-vowel
# syllable counting, English stopword/weak-phrase lists, formula constants
# (206.835, 84.6, 1.015) fit to English syllable statistics. None of that is
# meaningful for Tamil, Devanagari, or Hangul script — confirmed during the
# i18n investigation that these previously produced a numerically-plausible
# but semantically meaningless score (near-ceiling, since the vowel regex
# matches ~0 characters in non-Latin text). is_latin_script() is the single
# gate every one of those checks uses before running; when it's False the
# caller must skip the check and say so explicitly rather than fabricate a
# number.
# ─────────────────────────────────────────────────────────────────────────────
_NON_LATIN_SCRIPT_RANGES: list[tuple[int, int]] = [
    (0x0B80, 0x0BFF),  # Tamil
    (0x0900, 0x097F),  # Devanagari (Hindi, Marathi, ...)
    (0xAC00, 0xD7A3),  # Hangul syllables (Korean)
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x0600, 0x06FF),  # Arabic
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x30FF),  # Hiragana / Katakana
]


def script_profile(text: str) -> dict[str, int]:
    """Count alphabetic characters by broad script family. Non-alphabetic
    characters (digits, punctuation, emoji, whitespace) are ignored, so a
    Tamil sentence with an English brand name or a number in it is still
    correctly read as majority-Tamil."""
    latin = 0
    non_latin = 0
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _NON_LATIN_SCRIPT_RANGES):
            non_latin += 1
        elif ch.isalpha():
            latin += 1
    return {"latin": latin, "non_latin": non_latin}


def is_latin_script(text: str) -> bool:
    """True when text's alphabetic characters are majority Latin-script.

    Content with no alphabetic characters at all (empty, digits/emoji/
    punctuation only) is treated as Latin by convention — there's nothing
    script-specific to get wrong, and the callers below already special-case
    empty content separately.
    """
    profile = script_profile(text)
    total = profile["latin"] + profile["non_latin"]
    if total == 0:
        return True
    return profile["latin"] >= profile["non_latin"]

READABILITY_THRESHOLDS = {
    Platform.BLOG: 45,
    Platform.NEWSLETTER: 50,
    Platform.LINKEDIN: 55,
    Platform.TWITTER: 65,
    Platform.TWITTER_THREAD: 65,
    Platform.INSTAGRAM: 65,
    Platform.FACEBOOK: 55,
    Platform.YOUTUBE: 50,
}

CTA_MARKERS = [
    "?", "comment", "share", "follow", "subscribe", "link in bio",
    "check out", "read more", "click", "dm me", "let me know",
    "what do you think", "tag someone", "save this",
    "try", "open", "start", "use", "this sunday",  
]

WEASEL_WORDS = ["very", "really", "quite", "rather", "somewhat", "fairly", "basically", "literally"]
PASSIVE_PATTERNS = [r"\bwas \w+ed\b", r"\bwere \w+ed\b", r"\bis being\b", r"\bbeen \w+ed\b"]


def flesch_reading_ease(text: str) -> Optional[float]:
    """Approximate Flesch Reading Ease without external library.

    Returns ``None`` — not a fabricated number — when the text is majority
    non-Latin script. The formula's syllable proxy counts ASCII vowels
    (``[aeiouAEIOU]+``); for Tamil/Devanagari/Hangul that counts ~0
    syllables regardless of actual content, which previously produced a
    score near the ceiling (100) that looked plausible but meant nothing.
    Callers must treat None as "not evaluated", never as "score is 0".
    """
    if not is_latin_script(text):
        return None
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    words = text.split()
    if not sentences or not words:
        return 0.0
    avg_sentence_len = len(words) / len(sentences)
    syllables = sum(len(re.findall(r"[aeiouAEIOU]+", w)) for w in words)
    avg_syllables = syllables / len(words)
    score = 206.835 - (1.015 * avg_sentence_len) - (84.6 * avg_syllables)
    return max(0.0, min(100.0, score))


def check_banned_words(content: str, banned_words: list[str]) -> list[str]:
    return [w for w in banned_words if w.lower() in content.lower()]


def check_grammar_advisory(content: str) -> list[str]:
    """
    Advisory grammar check. Never blocks content — warning only.
    Checks passive voice and weasel words.
    Maps to extras.grammarCheck toggle.
    """
    issues = []
    for pattern in PASSIVE_PATTERNS:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            issues.append(f"Advisory: Passive voice found — '{matches[0]}'")
    for word in WEASEL_WORDS:
        if f" {word} " in content.lower():
            issues.append(f"Advisory: Weasel word — '{word}'")
    return issues


def check_twitter_limits(content: str, platform: Platform) -> list[str]:
    """Hard check for Twitter character limits."""
    issues = []
    if platform == Platform.TWITTER:
        if len(content) > 280:
            issues.append(f"Tweet exceeds 280 characters ({len(content)} chars)")
    elif platform == Platform.TWITTER_THREAD:
        tweets = [t.strip() for t in content.split("\n") if t.strip() and re.match(r"^\d+/", t.strip())]
        for tweet in tweets:
            clean = re.sub(r"^\d+/\s*", "", tweet)
            if len(clean) > 280:
                issues.append(f"Thread tweet exceeds 280 chars: '{clean[:40]}...'")
    return issues


def check_minimum_length(content: str, platform: Platform) -> list[str]:
    MIN_WORD_COUNTS = {
        Platform.BLOG: 600,
        Platform.NEWSLETTER: 200,
        Platform.LINKEDIN: 150,
        Platform.TWITTER: 0,          
        Platform.TWITTER_THREAD: 150,
        Platform.INSTAGRAM: 80,
        Platform.FACEBOOK: 100,
        Platform.YOUTUBE: 80,
    }

    issues = []
    minimum = MIN_WORD_COUNTS.get(platform, 15)
    word_count = len(content.split())

    if minimum > 0 and word_count < minimum:
        issues.append(
            f"Content too short for {platform.value}: {word_count} words (minimum {minimum})"
        )

    # Twitter char-based minimum
    if platform == Platform.TWITTER and len(content) < 200:
        issues.append(
            f"Content too short for Twitter: {len(content)} chars (minimum 200)"
        )

    return issues

async def run_quality_gate(
    content: str,
    platform: Platform,
    brand_context: str,
    banned_words: list[str],
    avoid_blacklist: bool = True,
    grammar_check: bool = False,
) -> QualityResult:
    """
    Run all quality checks. Returns QualityResult.
    Hard failures (banned words, twitter limits, minimum length) set passed=False.
    Advisory issues (grammar, readability, CTA) are included in issues but do not block.
    """
    hard_issues = []
    advisory_issues = []

    if avoid_blacklist and banned_words:
        found = check_banned_words(content, banned_words)
        if found:
            hard_issues.append(f"Banned words found: {', '.join(found)}")

    twitter_issues = check_twitter_limits(content, platform)
    hard_issues.extend(twitter_issues)

    length_issues = check_minimum_length(content, platform)
    hard_issues.extend(length_issues)

    readability = flesch_reading_ease(content)
    threshold = READABILITY_THRESHOLDS.get(platform, 45)
    if readability is None:
        advisory_issues.append(
            "Advisory: Readability scoring not available for this content's language "
            "(non-Latin script) — not evaluated, not penalised."
        )
    elif readability < threshold:
        advisory_issues.append(f"Advisory: Readability score {readability:.0f} below {threshold} for {platform.value}")

    cta_markers_lower = [m.lower() for m in CTA_MARKERS]
    content_lower = content.lower()
    if platform not in (Platform.BLOG, Platform.TWITTER) and not any(m in content_lower for m in cta_markers_lower):
        advisory_issues.append(f"Advisory: No CTA detected for {platform.value}")

    if grammar_check:
        grammar_issues = check_grammar_advisory(content)
        advisory_issues.extend(grammar_issues)

    all_issues = hard_issues + advisory_issues
    passed = len(hard_issues) == 0

    return QualityResult(
        passed=passed,
        issues=all_issues,
        content=content,
        readability_score=readability,
    )
