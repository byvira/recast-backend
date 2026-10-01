"""Finds claims in generated text that nothing the member supplied backs up.

A model asked to be specific tends to invent figures, prices, scores and "last Thursday" stories. This check does not
judge whether a claim is true. It only asks: does this number, or this dated event, appear in the source content or
the brand facts the model was given? If not, it is listed as a warning so the member can confirm or remove it before
publishing. It never blocks anything and never edits the text.

Used by every text flow that writes or rewrites member-facing copy (generation, repurposing, quick edits, scripts)."""
from __future__ import annotations

import re
import unicodedata

_MAX_REPORTED = 8

_PERCENT = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?(?:%|percent\b|per cent\b)", re.I)
_MONEY = re.compile(r"(?:[₹$€£]|\b(?:rs\.?|inr|usd|eur|gbp)\s?)\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m|lakh|lakhs|crore|crores|million|billion)?", re.I)
_MONEY_AFTER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(rupees|dollars|euros|pounds)\b", re.I)
_DURATION = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)[\s-]?(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)\b", re.I)
_MULTIPLE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?(?:x|times)\b", re.I)
_SCORE = re.compile(r"(?<![\w.,])(\d{1,2}\.\d{1,2})(?![\w%.])")
_DATED_EVENT = re.compile(
    r"\b(?:last|this past|earlier this)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|weekend|month|quarter|year)\b"
    r"|\b(?:yesterday|two days ago|three days ago|a few days ago|a week ago|last night)\b",
    re.I,
)
_ANECDOTE = re.compile(
    r"\bI (?:ran|tested|tried|asked|fed|compared|spoke|talked|interviewed|measured|timed|sat down)\b[^.\n]{0,80}"
    r"|\b(?:a|one) (?:client|customer|founder|user|member|reader|colleague) (?:told|said|asked|shared|messaged|emailed)\b[^.\n]{0,60}",
    re.I,
)


def _digits_to_ascii(text: str) -> str:
    """Devanagari, Tamil and other digit forms read as the same numbers (so a Tamil post is judged like an English one)."""
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c for c in text)


def _num(raw: str) -> str:
    """A number as a comparable string: no thousands commas, no trailing zero decimals."""
    value = raw.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def _numbers_in(text: str) -> set[str]:
    return {_num(m) for m in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", _digits_to_ascii(text))}


def _claims_in(content: str) -> list[tuple[str, str]]:
    """(kind, text) for every figure or story-like claim in the content, in order."""
    text = _digits_to_ascii(content)
    found: list[tuple[int, str, str, str]] = []  # position, kind, claim text, number to look for
    for rx, kind in ((_PERCENT, "percentage"), (_MONEY, "price"), (_MONEY_AFTER, "price"), (_DURATION, "time or duration"), (_MULTIPLE, "multiplier"), (_SCORE, "score")):
        for m in rx.finditer(text):
            found.append((m.start(), kind, m.group(0).strip(), _num(m.group(1))))
    for m in _DATED_EVENT.finditer(text):
        found.append((m.start(), "dated event", m.group(0).strip(), ""))
    for m in _ANECDOTE.finditer(text):
        found.append((m.start(), "story that may not have happened", m.group(0).strip(), ""))
    found.sort(key=lambda f: f[0])
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for _, kind, claim, number in found:
        key = (kind, claim.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((kind, claim if not number else f"{claim}\x00{number}"))
    return out


def _mostly_in(claim: str, pool_low: str) -> bool:
    """A story or dated event is backed up when most of its meaningful words appear in the sources (a paraphrase of a real
    story still counts, an invented one does not)."""
    words = [w for w in re.findall(r"[^\W\d_]{4,}", claim.lower()) if w not in {"that", "this", "with", "from", "have", "last", "week", "earlier", "past"}]
    if not words:
        return claim.lower() in pool_low
    hits = sum(1 for w in words if w in pool_low)
    return hits / len(words) >= 0.6


def unsupported_claims(content: str, sources: list[str]) -> list[str]:
    """Human readable warnings for claims in `content` that no text in `sources` backs up. Empty list means nothing to flag.

    A figure counts as backed up when the same number appears anywhere in the sources. A dated event or a story is
    backed up when the same words appear. Nothing in `sources` means there is nothing to compare with, so nothing is flagged."""
    pool = " ".join(s for s in sources if s)
    if not pool.strip() or not (content or "").strip():
        return []
    known = _numbers_in(pool)
    pool_low = _digits_to_ascii(pool).lower()
    warnings: list[str] = []
    for kind, raw in _claims_in(content):
        claim, _, number = raw.partition("\x00")
        if number:
            if number in known:
                continue
        elif _mostly_in(claim, pool_low):
            continue
        warnings.append(f"Not in your source or brand facts ({kind}): \"{claim[:80]}\"")
        if len(warnings) >= _MAX_REPORTED:
            break
    return warnings
