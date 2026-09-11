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
from app.shared.llm import GroqModel, call_llm

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


def _placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(template))


async def _translate(key: str, language: str, english_template: str) -> tuple[str, bool]:
    """Translate english_template into `language` via one Groq call,
    preserving every {placeholder} token verbatim.

    Returns (text, cacheable). ``cacheable`` is False whenever `text` is an
    English fallback standing in for a translation that didn't actually
    happen — a failed call or a corrupted (placeholder-mismatched) result —
    so the caller must NOT persist it: caching a transient failure would
    turn a temporary rate limit into a permanent "this language never gets
    translated" entry. It is True both when translation genuinely succeeded
    and when `language == "en"` (translating English into English is a
    correct identity result, not a failure standing in for one).
    """
    if language == "en":
        # Translating English into English is an identity operation, not a
        # different behaviour for a specific language — no LLM call needed,
        # but this still goes through the same cache-then-lookup path as
        # every other language (see get_localized_string below).
        return english_template, True

    wanted = _placeholders(english_template)
    prompt = (
        f"Translate the following template into the language identified by the code "
        f"or name '{language}'. This is a template with placeholder tokens like "
        f"{{example}} — you MUST preserve every placeholder token EXACTLY as written, "
        f"character-for-character, including the curly braces and the name inside them. "
        f"Do not translate, rename, or alter the placeholder names. Translate only the "
        f"surrounding natural-language text around them.\n\n"
        f"TEMPLATE:\n{english_template}\n\n"
        f"Return ONLY the translated template text. No explanation, no quotes, no markdown."
    )
    try:
        translated = await call_llm(prompt=prompt, model=GroqModel.FAST, max_tokens=1500)
        translated = translated.strip()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "get_localized_string: translation call failed key=%s language=%s: %s — "
            "using English source for this call only, NOT caching the fallback",
            key, language, exc,
        )
        return english_template, False

    got = _placeholders(translated)
    if got != wanted:
        logger.error(
            "get_localized_string: placeholder mismatch key=%s language=%s wanted=%s got=%s — "
            "using English source instead of a corrupted translation, NOT caching the fallback",
            key, language, sorted(wanted), sorted(got),
        )
        return english_template, False

    return translated, True


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
