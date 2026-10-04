"""Plain-text cleanup and rule-based screening for generated content.

Everything here is free to run (no model call) and synchronous, so it can sit in the publish gate and in the shared
model wrappers. `clean_text` removes the marks that read as machine written; `screen_text` says whether the text
is safe to put in front of people and why not.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

# ── Cleanup ───────────────────────────────────────────────────────────────────

_EM_DASH = re.compile(r"\s*[—―﹘﹣－]\s*")
# An en dash with spaces around it is used as a pause. Between digits or words with no spaces it is a range and stays.
_SPACED_EN_DASH = re.compile(r"\s+–\s+")
_INVISIBLE = re.compile("[​‌‍⁠﻿­]")

# Opening and closing lines a chat model adds around the answer.
_PREAMBLE = re.compile(
    r"\A\s*(?:(?:sure|certainly|absolutely|of course)\b[^\n]{0,60}?[!:.]\s*"
    r"|here(?:'s| is| are)\s+(?:your|the|a|an)\s+(?:\w+\s+){0,3}?(?:post|draft|rewrite|rewritten|version|caption|thread|tweet|copy|text)\b[^\n]{0,60}:\s*)\n+",
    re.IGNORECASE,
)
_POSTAMBLE = re.compile(
    r"\n+\s*(?:i hope this helps|hope this helps|let me know if[^\n]*|feel free to[^\n]*)[^\n]*\s*\Z",
    re.IGNORECASE,
)

# Words that are nearly always filler. Each maps to a plainer wording that keeps the sentence grammatical.
_PLAIN_WORDING: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), replacement)
    for pattern, replacement in (
        (r"\bdelves into\b", "digs into"),
        (r"\bdelve into\b", "dig into"),
        (r"\bdelving into\b", "digging into"),
        (r"\bin today's fast-paced world,?\s*", ""),
        (r"\bin the ever-evolving (?:world|landscape) of\b", "in"),
        (r"\bit(?:'s| is) important to note that\s*", ""),
        (r"\bit(?:'s| is) worth noting that\s*", ""),
    )
)


def strip_dashes(text: str) -> str:
    """Replace em dashes (and spaced en dashes) with a comma, keeping the word boundary and tidy punctuation."""
    if not text or not re.search("[–—―﹘﹣－]", text):
        return text
    out = _EM_DASH.sub(", ", text)
    out = _SPACED_EN_DASH.sub(", ", out)
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    out = re.sub(r",\s*([.!?])", r"\1", out)
    return out.strip()


def clean_text(text: str, *, plain_wording: bool = True) -> str:
    """The text without dashes, invisible characters, chat preambles and filler wording."""
    if not isinstance(text, str) or not text:
        return text
    out = _INVISIBLE.sub("", text).replace(" ", " ")
    out = _PREAMBLE.sub("", out, count=1)
    out = _POSTAMBLE.sub("", out, count=1)
    if plain_wording:
        for pattern, replacement in _PLAIN_WORDING:
            out = pattern.sub(replacement, out)
    out = strip_dashes(out)
    return out.strip() if out != text else out


def clean_value(value: Any) -> Any:
    """`clean_text` applied to every string inside a dict or list, for structured model replies."""
    if isinstance(value, str):
        return clean_text(value, plain_wording=False)
    if isinstance(value, list):
        return [clean_value(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_value(v) for k, v in value.items()}
    return value



# ── Seeing through disguises ─────────────────────────────────────────────────

# Look-alike letters from other alphabets and the usual number and symbol swaps, folded to plain letters.
_LOOKALIKES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ѕ": "s", "ј": "j",
    "α": "a", "ο": "o", "ρ": "p", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "τ": "t",
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "@": "a", "$": "s", "!": "i", "|": "i", "€": "e", "£": "l",
})
_SPACED_LETTERS = re.compile(r"\b(?:[a-z][\s.\-_*]){2,}[a-z]\b")
_REPEATS = re.compile(r"(.)\1{2,}")


def content_hash(text: str) -> str:
    """A short fingerprint of the text, used to remember that this exact text was already checked."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def disguise_variants(text: str) -> list[str]:
    """The text as written plus a version with the usual disguises undone (look-alike letters, number and symbol swaps,
    stretched letters, letters split by spaces or dots), so "f u c k", "f*ck" and "fuuuck" are read as the word."""
    base = unicodedata.normalize("NFKC", _INVISIBLE.sub("", text or "")).lower()
    folded = base.translate(_LOOKALIKES)
    folded = _SPACED_LETTERS.sub(lambda m: re.sub(r"[\s.\-_*]", "", m.group(0)), folded)
    folded = _REPEATS.sub(r"\1\1", folded)
    stars = re.sub(r"(?<=\w)[*#%]+(?=\w)", "", folded)
    singles = re.sub(r"(.)\1+", r"\1", stars)
    variants = [base]
    for candidate in (folded, stars, singles):
        if candidate not in variants:
            variants.append(candidate)
    return variants


_lexicon = None


def _profanity_lexicon():
    """The maintained word list from the `better-profanity` package (loaded once). None if the package is missing, in
    which case the built-in lists and the model check still apply."""
    global _lexicon
    if _lexicon is None:
        try:
            from better_profanity import Profanity

            _lexicon = Profanity()
            _lexicon.load_censor_words()
        except Exception:
            _lexicon = False
    return _lexicon or None


def lexicon_hits(text: str) -> list[str]:
    """Words from the maintained profanity and slur list found in the text, seeing through disguises."""
    lexicon = _profanity_lexicon()
    if not lexicon:
        return []
    found: list[str] = []
    for variant in disguise_variants(text):
        if lexicon.contains_profanity(variant):
            tokens = re.findall(r"[a-z']+", variant)
            found.extend(t for t in tokens if lexicon.contains_profanity(t) and t not in found)
    return found[:10]


_RISK_TOPICS = re.compile(
    r"\b(?:kill|suicide|self[- ]harm|rape|abuse|terror|bomb|weapon|nazi|genocide|slave|racist|racism|sexist|porn|nude|sex|drug|cocaine|"
    r"heroin|molest|pedophile|explicit|hate|slur|threat)\w*",
    re.IGNORECASE,
)


def risk_signals(text: str) -> list[str]:
    """Signs a text may need a closer read by the model: a hit from the profanity and slur list, or a sensitive topic."""
    signals = [f"word: {w}" for w in lexicon_hits(text)]
    signals += [f"topic: {m.group(0).lower()}" for m in _RISK_TOPICS.finditer(text or "")][:5]
    return signals


# ── Screening ─────────────────────────────────────────────────────────────────

CATEGORIES = {
    "sexual": "Sexual or adult content",
    "hate": "Hateful content",
    "violence": "Violence or threats",
    "self_harm": "Encouraging self harm",
    "profanity": "Strong language",
    "assistant_leak": "Text left over from the writing assistant",
    "custom": "A term blocked by Recast staff",
}

# Explicit adult terms: blocked at every strictness.
_SEXUAL_EXPLICIT = (
    "porn", "porno", "pornography", "xxx", "hentai", "blowjob", "handjob", "cumshot", "gangbang", "nsfw",
    "sex tape", "sex video", "explicit sex",
)
# Added at the strict level: not always unsafe on their own (for example a shade called "nude").
_SEXUAL_STRICT = ("nude", "nudes", "naked", "erotic", "orgasm", "onlyfans", "fetish")
_PROFANITY_STRICT = (
    "fuck", "fucking", "fucked", "shit", "bullshit", "bitch", "asshole", "bastard", "dickhead", "piss off",
)

_HATE_PATTERNS = (
    r"\b(?:all|those|these|every)\s+(?:\w+\s+){0,2}(?:should|must|deserve to)\s+(?:die|be killed|be exterminated|burn)\b",
    r"\b(?:gas|exterminate|eradicate|wipe out)\s+(?:all\s+)?(?:the\s+)?(?:jews|muslims|blacks|gays|immigrants|women)\b",
    r"\bwhite\s+(?:power|supremacy)\b",
    r"\bheil\s+hitler\b",
    r"\brace\s+traitors?\b",
    r"\bgo\s+back\s+to\s+your\s+(?:own\s+)?country\b",
    r"\bsubhuman\b",
)
_VIOLENCE_PATTERNS = (
    r"\bi(?:'ll| will| am going to|'m going to)\s+(?:kill|murder|shoot|stab|hurt|beat)\s+(?:you|him|her|them|everyone)\b",
    r"\b(?:kill|murder|shoot|stab)\s+(?:him|her|them|everyone|all of them)\b",
    r"\bdeath\s+threats?\b",
    r"\bhow\s+to\s+(?:make|build)\s+a\s+(?:bomb|pipe bomb|explosive)\b",
    r"\bmass\s+shooting\b.{0,30}\b(?:plan|next|do it)\b",
)
_SELF_HARM_PATTERNS = (
    r"\byou\s+should\s+(?:just\s+)?(?:kill\s+yourself|end\s+it|die)\b",
    r"\bways\s+to\s+(?:kill|hurt|harm)\s+yourself\b",
    r"\bhow\s+to\s+(?:kill|hurt|harm)\s+yourself\b",
    r"\b(?:kill\s+yourself|kys)\b",
)
# A model that refuses or talks about itself must never have that text published.
_LEAK_PATTERNS = (
    r"\bas\s+an?\s+(?:ai|artificial intelligence)(?:\s+language)?\s+model\b",
    r"\bas\s+a\s+language\s+model\b",
    r"\bi(?:'m| am)\s+(?:sorry|unable),?\s+(?:but\s+)?i\s+(?:can(?:'|no)t|cannot|am unable to)\s+(?:help|assist|comply|provide|fulfill)\b",
    r"\bi\s+(?:can(?:'|no)t|cannot)\s+(?:assist|help)\s+with\s+that\b",
    r"\bi\s+(?:can(?:'|no)t|cannot)\s+(?:create|write|generate)\s+(?:that|this)\s+content\b",
    r"\[(?:insert|your|add)\s[^\]]{2,40}\]",
)

DEFAULT_AI_PHRASES = (
    "in today's fast-paced world",
    "in the ever-evolving landscape",
    "it's important to note",
    "unlock the power of",
    "navigate the complexities",
    "game-changer",
    "dive deep into",
    "a testament to",
    "tapestry of",
    "at the end of the day",
)


@dataclass
class ScreenResult:
    ok: bool = True
    categories: list[str] = field(default_factory=list)
    matches: list[str] = field(default_factory=list)

    def message(self) -> str:
        if self.ok:
            return ""
        names = ", ".join(CATEGORIES.get(c, c).lower() for c in self.categories)
        return f"This post was held back because it may contain {names}. Edit it or ask for a rewrite."


def _terms_regex(terms: Iterable[str]) -> re.Pattern[str] | None:
    cleaned = sorted({t.strip().lower() for t in terms if t and t.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(t) for t in cleaned) + r")(?![\w])", re.IGNORECASE)


def _compile(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns]


_HATE = _compile(_HATE_PATTERNS)
_VIOLENCE = _compile(_VIOLENCE_PATTERNS)
_SELF_HARM = _compile(_SELF_HARM_PATTERNS)
_LEAK = _compile(_LEAK_PATTERNS)


def _allowed_spans(text: str, allowed_terms: Iterable[str]) -> list[tuple[int, int]]:
    pattern = _terms_regex(allowed_terms)
    return [m.span() for m in pattern.finditer(text)] if pattern else []


def _inside(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(a <= span[0] and span[1] <= b for a, b in spans)


def screen_text(
    text: str,
    *,
    strictness: str = "standard",
    extra_blocked_terms: Iterable[str] = (),
    allowed_terms: Iterable[str] = (),
) -> ScreenResult:
    """Whether `text` is safe to show people. Terms Ops lists as allowed are never counted as matches."""
    if not isinstance(text, str) or not text.strip():
        return ScreenResult()
    allowed = _allowed_spans(text, allowed_terms)
    found: dict[str, list[str]] = {}

    def hit(category: str, match: re.Match[str], allowed_here: list[tuple[int, int]]) -> None:
        if _inside(match.span(), allowed_here):
            return
        word = match.group(0).strip()[:60]
        if word not in found.setdefault(category, []):
            found[category].append(word)

    strict = strictness == "strict"
    sexual = _terms_regex(_SEXUAL_EXPLICIT + (_SEXUAL_STRICT if strict else ()))
    profane = _terms_regex(_PROFANITY_STRICT) if strict else None
    custom = _terms_regex(extra_blocked_terms)
    for index, variant in enumerate(disguise_variants(text)):
        spans = allowed if index == 0 else _allowed_spans(variant, allowed_terms)
        if sexual:
            for m in sexual.finditer(variant):
                hit("sexual", m, spans)
        if profane:
            for m in profane.finditer(variant):
                hit("profanity", m, spans)
        for category, patterns in (("hate", _HATE), ("violence", _VIOLENCE), ("self_harm", _SELF_HARM), ("assistant_leak", _LEAK)):
            for pattern in patterns:
                for m in pattern.finditer(variant):
                    hit(category, m, spans)
        if custom:
            for m in custom.finditer(variant):
                hit("custom", m, spans)
    if strict:
        # A word is allowed when Ops allowed it on its own or as part of an allowed phrase ("nude lipstick" allows "nude").
        allowed_words = {w for t in allowed_terms if t for w in re.findall(r"[a-z']+", t.lower())}
        for word in lexicon_hits(text):
            if word not in allowed_words:
                found.setdefault("profanity", [])
                if word not in found["profanity"]:
                    found["profanity"].append(word)

    if not found:
        return ScreenResult()
    return ScreenResult(
        ok=False,
        categories=sorted(found),
        matches=[m for category in sorted(found) for m in found[category]][:10],
    )


def ai_phrases_in(text: str, phrases: Iterable[str] = DEFAULT_AI_PHRASES) -> list[str]:
    """The listed machine sounding phrases that appear in `text`. Reported, never silently rewritten."""
    if not isinstance(text, str) or not text:
        return []
    lowered = text.lower().replace("’", "'")
    return [p for p in phrases if p and p.lower() in lowered]
