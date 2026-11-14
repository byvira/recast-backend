"""Which language, and which script, Ask answers in.

A request is answered in the language the person is using, and a conversation keeps its language: a short follow-up such as "ok do it" has too few words
for a detector to place, so it takes the language the conversation was already in. The script is remembered too: a person who writes Tamil in English
letters (Tanglish) is answered the same way, and one who writes Tamil letters is answered in Tamil letters. The choice is `choose` (no I/O); `resolve`
reads the conversation and the member's own default, and `remember` writes the result back for the next turn."""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.shared.language import detect_language, user_language
from app.shared.language_profile import _SCRIPT_LANGUAGE, script_of

logger = logging.getLogger(__name__)

#: a message this short does not say enough for the detector to overrule the language the conversation is already in
SHORT_MESSAGE_WORDS = 6


def _base(code: str) -> str:
    return (code or "").strip().lower().split("+")[0].split("-")[0].split("_")[0]


def choose(text: str, detected: Optional[str], remembered: str = "", remembered_script: str = "", default: str = "") -> tuple[str, str]:
    """(language, script) for this message. `script` is "native" (the language's own letters) or "latin" (English letters).
    Order: the letters used, then what the detector is sure of, then what the conversation was already using for short or unplaceable text, then the
    member's own default, then nothing (the caller answers in the request's own language)."""
    letters, _ = script_of(text)
    if letters != "latin":
        return _SCRIPT_LANGUAGE.get(letters, "unknown"), "native"
    words = len((text or "").split())
    kept = remembered if _base(remembered) not in ("", "en") else ""
    if detected and _base(detected) != "en":
        # a short message that the detector places in another language than the conversation has used is more likely a slip of the detector than a change
        if kept and words <= SHORT_MESSAGE_WORDS and _base(detected) != _base(kept):
            return kept, remembered_script or "latin"
        return detected, "latin"
    if kept and words <= SHORT_MESSAGE_WORDS:
        return kept, remembered_script or "latin"
    if detected:                                    # English, in a message long enough to be sure of
        return detected, "latin"
    if kept:
        return kept, remembered_script or "latin"
    if default and _base(default) != "en":
        return default, "latin"
    return "", "latin"


async def resolve(text: str, thread: Optional[dict[str, Any]], user_id: str) -> tuple[str, str]:
    """`choose` with what the conversation and the member's account say."""
    default = ""
    try:
        default = await user_language(user_id) or ""
    except Exception:  # noqa: BLE001 - a lookup problem never stops a request
        default = ""
    t = thread or {}
    return choose(text, detect_language(text), str(t.get("language") or ""), str(t.get("script") or ""), default)


async def remember(thread_id: Optional[str], language: str, script: str) -> None:
    """Keep the language and script on the conversation for the next turn. English and unknown are not kept, so one English word never resets a conversation."""
    if not thread_id or _base(language) in ("", "en", "unknown"):
        return
    try:
        from app.ask import threads

        await threads.threads().update_one({"id": thread_id, "$or": [{"language": {"$ne": language}}, {"script": {"$ne": script}}]}, {"$set": {"language": language, "script": script}})
    except Exception:  # noqa: BLE001
        logger.debug("Could not keep the conversation's language")


def rule(language: str, script: str) -> str:
    """The sentence that tells a model which language and script to write in. Empty for English or when nothing is known."""
    if _base(language) in ("", "en"):
        return ""
    if script == "latin":
        return f'Write in the language with code "{language}" using English letters, the way the person writes it (not in the language\'s own script).'
    return f'Write in the language with code "{language}" in its own script.'


async def say(facts: str, language: str, script: str, think: Any) -> Optional[str]:
    """One or two natural sentences in the person's language and script that tell them `facts`, written by the model rather than translated from a template.
    None when the language is English or nothing came back, so the caller uses its fixed sentence."""
    how = rule(language, script)
    if not how:
        return None
    from app.ask import fence

    try:
        raw = await think("respond", f"{how} Say it warmly and plainly in one or two short sentences, as a helpful person would. Keep every number exactly. Return JSON {{\"text\": \"...\"}}.\n\nWhat to tell them:\n" + fence.fence(facts, 500), fence.RULES)
        out = fence.clean(str(raw.get("text") or ""), 400)
        return out or None
    except Exception:  # noqa: BLE001 - the fixed sentence is always correct
        return None
