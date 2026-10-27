"""Typing suggestions: the words that could come next in a text box, shown as grey text the member can accept or ignore.

Two sources, cheapest first. The member's own earlier prompts and their brand's own words need no AI call and are sent to the
page once, so matching them costs nothing while typing. Only when those have nothing to offer is one short continuation asked of the
free language model, and then only after a pause, once per distinct text, within a small daily allowance. Nothing here ever raises
to the member: a suggestion that cannot be made is simply not shown."""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Optional

#: What each kind of text box is for, so the continuation sounds like that kind of text.
KINDS: dict[str, str] = {
    "post_idea": "an idea or notes for a social media post",
    "image_prompt": "a description of a picture to be made",
    "image_text": "short words printed on a picture (a headline)",
    "description": "a short description or caption that explains something",
    "script": "a script to be read aloud",
    "post_edit": "a social media post being written or edited",
    "instruction": "an instruction telling an editor how to change a piece of writing",
    "brand_text": "a sentence describing a brand, its audience or its voice",
}

#: The most words one suggestion may have, and the least typed before one is asked for.
MAX_SUGGESTION_WORDS = 14
MIN_CHARS_FOR_AI = 8

_CACHE_SIZE = 500
_CACHE_SECONDS = 15 * 60
_cache: "OrderedDict[tuple, tuple[float, Optional[str]]]" = OrderedDict()


def cached(key: tuple) -> tuple[bool, Optional[str]]:
    """(found, value). A remembered "nothing to suggest" is a find too, so the same text is never asked about twice."""
    hit = _cache.get(key)
    if hit is None or time.monotonic() - hit[0] > _CACHE_SECONDS:
        _cache.pop(key, None)
        return False, None
    _cache.move_to_end(key)
    return True, hit[1]


def remember(key: tuple, value: Optional[str]) -> None:
    _cache[key] = (time.monotonic(), value)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)


def cache_key(user_id: str, kind: str, text: str) -> tuple:
    return (user_id, kind, " ".join(text.lower().split())[-300:])


def prompt_for(kind: str, text: str, brand_hint: str) -> tuple[str, str]:
    """(system, user) messages asking for the next few words only."""
    from app.prompts.registry import load_prompt

    what = KINDS.get(kind, "a piece of writing")
    system = load_prompt("suggest/complete_system", what=what, max_words=MAX_SUGGESTION_WORDS, brand_hint=brand_hint)
    return system, text


def clean_completion(raw: str, typed: str) -> Optional[str]:
    """The model's reply as a suggestion that can be appended to what was typed, or None when it cannot be used.

    The last typed word is taken as finished, so the suggestion starts a new word."""
    if not raw or not raw.strip():
        return None
    leading_space = raw[:1].isspace()
    lines = [line for line in raw.strip().splitlines() if line.strip()]
    out = lines[0].strip().strip('"“”').strip()
    if not out:
        return None
    # A reply that repeats the typed words (the model restating the text) has those words taken off the front.
    typed_clean = typed.rstrip()
    if typed_clean:
        low_out = out.lower()
        if low_out.startswith(typed_clean.lower()):
            out, leading_space = out[len(typed_clean):], True
        else:
            tail = " ".join(typed_clean.split()[-4:]).lower()
            if tail and low_out.startswith(tail):
                out, leading_space = out[len(tail):], True
    out = out.strip() if leading_space else out.rstrip()
    if not out:
        return None
    words = out.split()
    if len(words) > MAX_SUGGESTION_WORDS:
        out = " ".join(words[:MAX_SUGGESTION_WORDS])
    if re.search(r"\b(as an ai|i cannot|i can't|sorry)\b", out, flags=re.IGNORECASE):
        return None
    if not typed or typed[-1].isspace():
        return out.lstrip()
    # The last typed word is taken as finished, so the suggestion starts a new word, unless it opens with punctuation that
    # belongs to the word before it (a comma, a full stop, 's).
    if out[0] in ",.;:!?)’'-":
        return out
    return " " + out.lstrip().lstrip()
    if leading_space or typed[-1] in ".,!?;:)":
        return " " + out.lstrip()
    return out
