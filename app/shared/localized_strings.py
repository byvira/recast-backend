"""Runtime template translation, cached in MongoDB — replaces the static
en/ta/hi/ko dict templates in signals.py / personas.py / analytics/nodes.py
with support for any language, opaque, with zero validation against a fixed
set.

Design (per explicit direction — this is a real architectural change from
the static-dict approach used earlier in this session's i18n work, not a
patch on top of it):

  get_localized_string(key, language, english_template, ctx=None)

    1. Look up (key, language) in the ``localized_strings`` collection.
       - HIT: the cached translated template is used directly. No LLM call.
       - MISS: the english_template is translated into `language` via a
         single Groq call, the {placeholder} tokens are verified to have
         survived translation intact, and the result is persisted before
         being returned. Every subsequent call with the same (key, language)
         is then a cache hit — the LLM is only ever invoked once per template
         per language, not once per call.
    2. The (possibly-cached, possibly-freshly-translated) template is
       .format(**ctx)-ed and returned.

``language`` is never checked against any fixed set anywhere in this module
— any string is a valid cache key component. English is not special-cased
as a code branch: it still goes through the identical lookup-then-translate
path, the only difference being that "translating" English into English is
an identity operation, not a different code path.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.db.mongo import get_db
from app.prompts.registry import load_prompt
from app.shared.llm import GroqModel, call_llm

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")

# How many times one translation is attempted before falling back to the
# English source (uncached). A second try is cheap and fixes the usual
# one-off failure: the model echoing the prompt or answering in the wrong script.
_TRANSLATE_ATTEMPTS = 2

# Text that only ever appears if the model echoed the prompt back instead of
# (or around) the translation. Compared case-insensitively, and ignored when
# the English source itself legitimately contains the same words.
_LEAK_MARKERS = (
    "template:",
    "return only",
    "no explanation",
    "no quotes, no markdown",
    "translated template",
    "translated script",
    "<message>",
    "</message>",
    "<script>",
    "</script>",
    "treat it as text to localize",
    "reply with the localized message",
)

# A mixed-language ("xx+en") answer is written in English letters. A share of
# non-Latin letters above this means the model wrote the native script instead.
_MAX_NON_LATIN_RATIO = 0.15


def _placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(template))


def looks_leaked(text: str, source: str = "") -> bool:
    """True when `text` contains prompt or instruction wording that must never
    reach a user. Markers that also occur in `source` (the text we asked to
    translate) are not counted, so a message that genuinely mentions one of
    these phrases is not rejected."""
    low = (text or "").lower()
    src = (source or "").lower()
    return any(m in low and m not in src for m in _LEAK_MARKERS)


def non_latin_ratio(text: str) -> float:
    """Share of alphabetic characters outside the Latin alphabets (0.0 to 1.0)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) > 0x024F) / len(letters)


def clean_translation(text: str) -> str:
    """Trim whitespace and the wrapping the model sometimes adds (code fences,
    message tags, one pair of quotes) that is not part of the translation."""
    out = (text or "").strip()
    if out.startswith("```") and out.endswith("```"):
        out = out.strip("`").strip()
        first, _, rest = out.partition("\n")
        if first.strip().isalpha() and rest:
            out = rest.strip()
    for tag in ("<message>", "</message>"):
        out = out.replace(tag, "")
    out = out.strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'":
        out = out[1:-1].strip()
    return out


def _language_name(code: str) -> str:
    from app.models.text import LANGUAGE_NAMES

    normalised = (code or "").strip()
    known = LANGUAGE_NAMES.get(normalised.lower().split("-")[0])
    return known or f"the language identified by the code or name '{normalised}'"


def language_prompt_vars(language: str) -> dict[str, Any]:
    """Variables the translate prompt needs for `language`.

    A code with a "+" (ta+en, hi+en, es+en ...) is a deliberate mixed register
    such as Tanglish or Hinglish, not a request for pure translation: the
    non-English language is written in English letters and blended with
    English. Every other code is a plain single-language target.
    """
    code = (language or "").strip()
    if "+" in code:
        first, _, second = code.lower().partition("+")
        native_code, other_code = (second, first) if first.split("-")[0] == "en" else (first, second)
        return {
            "mixed": True, "language": code, "language_name": "",
            "native": _language_name(native_code), "other": _language_name(other_code),
        }
    return {
        "mixed": False, "language": code, "language_name": _language_name(code),
        "native": "", "other": "",
    }


def _reject_reason(text: str, source: str, wanted: set[str], mixed: bool) -> Optional[str]:
    if not text:
        return "empty output"
    if looks_leaked(text, source):
        return "prompt text leaked into the output"
    if _placeholders(text) != wanted:
        return f"placeholders changed (wanted {sorted(wanted)}, got {sorted(_placeholders(text))})"
    if mixed and non_latin_ratio(text) > _MAX_NON_LATIN_RATIO:
        return "mixed-language output was written in a non-Latin script"
    return None


async def _translate(key: str, language: str, english_template: str) -> tuple[str, bool]:
    """Localize english_template into `language` with one to two Groq calls,
    preserving every {placeholder} token verbatim.

    Returns (text, cacheable). ``cacheable`` is False whenever `text` is an
    English fallback standing in for a translation that didn't actually
    happen (every attempt failed or was rejected), so the caller must NOT
    persist it: caching a transient failure would turn a temporary rate limit
    into a permanent "this language never gets translated" entry. It is True
    both when a translation genuinely passed every check and when
    `language == "en"` (English into English is a correct identity result).

    An answer is rejected, and retried once, when it is empty, echoes prompt
    text (see looks_leaked), changes the placeholders, or (for mixed codes)
    comes back in a non-Latin script.
    """
    if language == "en":
        return english_template, True

    wanted = _placeholders(english_template)
    variables = language_prompt_vars(language)
    prompt = load_prompt(
        "fragments/translate_template", english_template=english_template, **variables
    )

    for attempt in range(1, _TRANSLATE_ATTEMPTS + 1):
        try:
            raw = await call_llm(
                prompt=prompt, model=GroqModel.FAST, max_tokens=1500,
                temperature=0.3 if attempt == 1 else 0.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "get_localized_string: translation call failed key=%s language=%s attempt=%d: %s",
                key, language, attempt, exc,
            )
            continue

        translated = clean_translation(raw)
        reason = _reject_reason(translated, english_template, wanted, variables["mixed"])
        if reason is None:
            return translated, True
        logger.error(
            "get_localized_string: rejected translation key=%s language=%s attempt=%d: %s",
            key, language, attempt, reason,
        )

    logger.error(
        "get_localized_string: using English source for key=%s language=%s after %d rejected "
        "attempts, NOT caching the fallback",
        key, language, _TRANSLATE_ATTEMPTS,
    )
    return english_template, False


async def get_localized_string(
    key: str,
    language: str,
    english_template: str,
    ctx: Optional[dict[str, Any]] = None,
) -> str:
    """Return english_template translated into `language`, formatted with ctx.

    `language` is a fully opaque string — no membership check against any
    known-languages list happens anywhere in this function or its callers.
    """
    db = get_db()
    coll = db["localized_strings"]

    doc = await coll.find_one({"key": key, "language": language})
    if doc is not None:
        logger.info(
            "get_localized_string: CACHE HIT key=%s language=%s — using cached translation, no LLM call",
            key, language,
        )
        template = doc["template"]
    else:
        logger.info(
            "get_localized_string: CACHE MISS key=%s language=%s — calling LLM to translate and caching result",
            key, language,
        )
        template, cacheable = await _translate(key, language, english_template)
        if cacheable:
            try:
                await coll.update_one(
                    {"key": key, "language": language},
                    {"$set": {
                        "key": key,
                        "language": language,
                        "template": template,
                        "source_template": english_template,
                        "updated_at": datetime.now(timezone.utc),
                    }, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
                    upsert=True,
                )
            except Exception as exc:  # noqa: BLE001
                # A cache-write failure must not break the caller — the translated
                # string is still returned this call, just re-translated next time.
                logger.error(
                    "get_localized_string: cache write failed key=%s language=%s: %s",
                    key, language, exc,
                )
        else:
            logger.info(
                "get_localized_string: NOT caching key=%s language=%s — the English "
                "fallback above stands in for a failed translation, not a real result; "
                "the next call will retry translation from scratch",
                key, language,
            )

    try:
        return template.format(**(ctx or {}))
    except (KeyError, IndexError) as exc:
        logger.error(
            "get_localized_string: format failed key=%s language=%s ctx_keys=%s: %s — "
            "returning unformatted template",
            key, language, list((ctx or {}).keys()), exc,
        )
        return template
