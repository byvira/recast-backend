"""Checks a suggested clip's quote against the real words of the recording. Pure."""

from __future__ import annotations

import re
from typing import Sequence

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [t.casefold() for t in _TOKEN.findall(text or "")]


def span_text(words: Sequence[dict], start_s: float, end_s: float) -> str:
    """The words spoken between start_s and end_s, as plain text."""
    return " ".join(
        str(w.get("word", "")).strip()
        for w in words
        if float(w.get("start_s") or 0) >= start_s - 0.05 and float(w.get("end_s") or 0) <= end_s + 0.05
    ).strip()


def verified_quote(quote: str, words: Sequence[dict], start_s: float, end_s: float, min_match: float = 0.7) -> str:
    """The quote itself when at least `min_match` of its words appear in that stretch of the recording,
    otherwise the recording's own words for that stretch. So nothing the model made up is ever shown."""
    actual = span_text(words, start_s, end_s)
    wanted = _tokens(quote)
    if not wanted:
        return actual
    have = set(_tokens(actual))
    matched = sum(1 for t in wanted if t in have)
    return quote.strip() if matched / len(wanted) >= min_match else actual
