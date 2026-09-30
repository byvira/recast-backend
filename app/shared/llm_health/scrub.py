"""Makes a provider's error text safe to store: no keys, tokens, emails or long ids, and a length cap.
Prompt and answer text never reach this module (only the provider's error message does), but providers
sometimes echo part of the request in an error, so the cap and the patterns are a second line of defence."""
from __future__ import annotations

import re

MAX_MESSAGE_CHARS = 500

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:gsk|sk|rk|pk|xai|hf|AIza)[-_A-Za-z0-9]{16,}"), "[key]"),
    (re.compile(r"(?i)\b(bearer|token|api[_ -]?key|authorization)\b\s*[:=]?\s*[-_.A-Za-z0-9]{12,}"), r"\1 [key]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "[id]"),
    (re.compile(r"\b\d{9,}\b"), "[number]"),
]


def scrub_message(text: object, limit: int = MAX_MESSAGE_CHARS) -> str:
    """The message with secrets removed, whitespace collapsed and cut to `limit` characters."""
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"
