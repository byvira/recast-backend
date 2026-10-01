"""Two small protections used by the prompt templates and the code that reads the answers.

fence():  puts text that came from outside (a member's paste, a scraped page, a transcript) inside a
          labelled block, removes any copy of the block's own tags from it so it cannot close the block
          early, and adds a line saying it is material to work from, not instructions.
guard_output():  the checks every model answer should pass before it is shown: not empty, not wrapped
          in a code fence or a JSON object, no chatty preamble, and none of our own instruction text.

Pure (no model call, no database).
"""

from __future__ import annotations

import json
import re
from typing import Optional

from app.prompts.fence import fence  # noqa: F401  (re-exported for callers)
from app.shared.localized_strings import clean_translation, looks_leaked, non_latin_ratio


_PREAMBLE = re.compile(
    r"^\s*(here(?:'s| is| are)[^\n:]{0,80}:|sure[,!.][^\n]{0,60}\n|certainly[,!.][^\n]{0,60}\n|of course[,!.][^\n]{0,60}\n)\s*",
    re.IGNORECASE,
)


def _unwrap_json(text: str, keys: tuple[str, ...]) -> str:
    """If the whole answer is a JSON object holding the text under a known key, return that text."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text
    try:
        data = json.loads(stripped)
    except ValueError:
        return text
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return text


def guard_output(
    text: object,
    *,
    source: str = "",
    json_keys: tuple[str, ...] = ("content", "text", "rewritten", "result"),
) -> Optional[str]:
    """The cleaned answer, or None when it should not be used. `source` is what we sent in, so a phrase
    that genuinely appears in the member's own text is not mistaken for a leak."""
    if not isinstance(text, str):
        return None
    out = clean_translation(text)
    out = _unwrap_json(out, json_keys)
    out = _PREAMBLE.sub("", out, count=1).strip()
    if not out or looks_leaked(out, source):
        return None
    return out


def clamp_score(value: object, default: float = 0.5) -> float:
    """A model's score as a number from 0 to 1. A percentage (87) is read as 0.87, anything above 100 is 1.0, below 0 is 0.0,
    and text that is not a number, nothing, or NaN becomes `default` (the middle), so one odd answer never crashes a run
    or shows as a wild score."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    if number < 0:
        return 0.0
    if number <= 1:
        return number
    if number <= 100:
        return number / 100
    return 1.0


def is_english(code: object) -> bool:
    """True for plain English only. A mixed code such as ta+en is not English for these purposes:
    the English-only style checks do not apply to it."""
    return str(code or "en").strip().lower().split("-")[0] == "en"


def contains_banned(text: str, words: object) -> list[str]:
    """Which banned words appear in `text`, ignoring letter case. Latin words must match as whole
    words; words in other scripts are matched anywhere, because word-boundary rules do not work
    for scripts with combining marks."""
    found: list[str] = []
    low = (text or "").casefold()
    for raw in words if isinstance(words, (list, tuple)) else []:
        word = str(raw or "").strip()
        if not word:
            continue
        key = word.casefold()
        if key.isascii():
            hit = re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", low) is not None
        else:
            hit = key in low
        if hit:
            found.append(word)
    return found


def hook_fits(content: str, hook: object, max_chars: int = 220) -> bool:
    """Whether a new opening line can replace the first line of `content`: one real line of sensible
    length, no instruction text, and in the same kind of script as the piece (an English hook must not
    land on top of Tamil writing, or the other way round)."""
    if not isinstance(hook, str):
        return False
    text = hook.strip()
    if not text or "\n" in text or len(text) > max_chars or looks_leaked(text, content):
        return False
    body, line = non_latin_ratio(content or ""), non_latin_ratio(text)
    if body > 0.5 and line < 0.3:
        return False
    if body < 0.1 and line > 0.5:
        return False
    return True
