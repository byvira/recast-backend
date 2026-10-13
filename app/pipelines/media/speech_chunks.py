"""Cutting a long script into parts a voice service can read in one request, at the places a speaker would pause.

The parts are cut at paragraph ends first, then at sentence ends, and only a single sentence longer than a part is cut between
words. Joining the parts back is the caller's job; nothing here changes a word."""
from __future__ import annotations

import re

#: The most characters sent to a voice service in one request. The fallback voice takes about 2,000, so this stays under it.
SPEECH_CHUNK_CHARS = 1900

_PARAGRAPHS = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?।。！？])\s+")


def _words_to_parts(sentence: str, limit: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for word in sentence.split():
        if len(word) > limit:
            # An unbroken run (a pasted link, a language written without spaces) is cut at the limit.
            if current:
                parts.append(current)
                current = ""
            parts.extend(word[i:i + limit] for i in range(0, len(word), limit))
            continue
        candidate = f"{current} {word}".strip()
        if len(candidate) > limit:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def split_for_speech(text: str, limit: int = SPEECH_CHUNK_CHARS) -> list[str]:
    """`text` as parts of at most `limit` characters, in order. A script that already fits comes back as one part."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    units: list[str] = []
    for paragraph in _PARAGRAPHS.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            units.append(paragraph)
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            units.extend([sentence] if len(sentence) <= limit else _words_to_parts(sentence, limit))

    parts: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if current and len(candidate) > limit:
            parts.append(current)
            current = unit
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
