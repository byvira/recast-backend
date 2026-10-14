"""Cutting a transcript into sentences, and the sentences into windows small enough for one model call.

A window never splits a sentence, and neighbouring windows share a few sentences so something said across a boundary is seen
whole at least once. Pure functions, no model and no database."""
from __future__ import annotations

import re
from typing import Any

# A sentence ends at one of these marks (Latin, Devanagari danda, Arabic question and full stop, ellipsis, Chinese and Japanese).
_END = re.compile(r"[.!?…।॥؟۔。！？][\"')\]]*$")
#: Pauses this long split a sentence even when the speaker used no punctuation.
PAUSE_SPLIT_MS = 1400
#: A sentence never grows past this many words, so a speaker who never stops still gives usable pieces.
MAX_SENTENCE_WORDS = 60
#: One window holds at most this many words (about 2,100 tokens), so the prompt and the answer fit the free per-minute ceiling.
WINDOW_WORDS = 1500
SHARED_SENTENCES = 2


def sentences(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group words into sentences: {"i", "start_ms", "end_ms", "text", "first", "last"} where first and last index the word list."""
    out: list[dict[str, Any]] = []
    first = 0
    for i, w in enumerate(words):
        last_in_run = i == len(words) - 1
        ends = bool(_END.search(w["w"].strip()))
        long_pause = (not last_in_run) and words[i + 1]["s_ms"] - w["e_ms"] >= PAUSE_SPLIT_MS
        too_long = (i - first + 1) >= MAX_SENTENCE_WORDS
        if ends or long_pause or too_long or last_in_run:
            run = words[first:i + 1]
            out.append({
                "i": len(out), "start_ms": run[0]["s_ms"], "end_ms": run[-1]["e_ms"],
                "text": " ".join(x["w"] for x in run).strip(), "first": first, "last": i,
                **({"speaker": run[0]["sp"]} if run[0].get("sp") else {}),
            })
            first = i + 1
    return out


def windows(sents: list[dict[str, Any]], max_words: int | None = None, shared: int | None = None) -> list[dict[str, Any]]:
    """Consecutive sentences grouped under the word limit: {"index", "from", "to"} (sentence numbers, inclusive)."""
    max_words = max_words or WINDOW_WORDS
    shared = SHARED_SENTENCES if shared is None else shared
    out: list[dict[str, Any]] = []
    start = 0
    n = len(sents)
    while start < n:
        count, end = 0, start
        while end < n:
            size = len(sents[end]["text"].split())
            if count and count + size > max_words:
                break
            count += size
            end += 1
        out.append({"index": len(out), "from": start, "to": end - 1})
        if end >= n:
            break
        start = max(start + 1, end - shared)
    return out


def window_text(sents: list[dict[str, Any]], win: dict[str, Any]) -> str:
    """The sentences of one window as numbered lines with their start time, the form the prompts read."""
    lines = []
    for s in sents[win["from"]:win["to"] + 1]:
        seconds = s["start_ms"] // 1000
        stamp = f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        who = f" [{s['speaker']}]" if s.get("speaker") else ""
        lines.append(f"{s['i']} ({stamp}){who} {s['text']}")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """A rough token count (about 1.4 tokens a word, more for scripts that split into many pieces), for pacing only."""
    return int(len(text.split()) * 1.4) + 40
