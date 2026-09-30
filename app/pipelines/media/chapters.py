"""Chapters for a recording: checking what the AI proposed against the real transcript.

The AI only chooses where topics change and what to call them. Everything that reaches
the member passes through `normalize_chapters`, so a made-up time, a duplicate, a crowd of
tiny chapters or a title that echoes the prompt can never appear. Pure, no network.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

MAX_CHAPTERS = 12
MIN_GAP_SECONDS = 15.0     # chapters closer together than this are merged into one
MIN_AUDIO_SECONDS = 60.0   # shorter recordings are not split into chapters
MAX_TITLE_CHARS = 80

_LEAK_MARKERS = ("return only", "no explanation", "start_s", "{\"chapters\"")


def clean_title(raw: Any) -> str:
    """One short plain line, or "" when it is empty or looks like prompt text."""
    text = " ".join(str(raw or "").split()).strip(" \"'`*#-")
    if not text:
        return ""
    low = text.lower()
    if any(marker in low for marker in _LEAK_MARKERS):
        return ""
    return text[:MAX_TITLE_CHARS].rstrip()


def normalize_chapters(
    items: Sequence[Any] | None,
    duration_s: float,
    *,
    max_chapters: int = MAX_CHAPTERS,
    min_gap: float = MIN_GAP_SECONDS,
) -> list[dict]:
    """Keep only believable chapters: {title, start_s}, sorted, the first at 0.

    Dropped: entries without a number or a title, a start outside the recording, a start
    within `min_gap` of the previous kept one. The first chapter is always moved to 0:00 so
    the whole recording is covered. Returns [] when nothing usable is left, or when the
    recording is too short to split.
    """
    if duration_s < MIN_AUDIO_SECONDS:
        return []

    parsed: list[tuple[float, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_s"))
        except (TypeError, ValueError):
            continue
        title = clean_title(item.get("title"))
        if not title or start != start or start < 0 or start >= duration_s:
            continue
        parsed.append((start, title))

    parsed.sort(key=lambda p: p[0])
    kept: list[dict] = []
    for start, title in parsed:
        if kept and start - kept[-1]["start_s"] < min_gap:
            continue
        kept.append({"title": title, "start_s": round(start, 2)})
        if len(kept) >= max_chapters:
            break

    if not kept:
        return []
    kept[0]["start_s"] = 0.0
    return kept


def transcript_for_prompt(words: Sequence[dict], every_seconds: float = 10.0) -> list[dict]:
    """The transcript cut into short timed lines ({start_s, text}) so the prompt stays small.

    A line is closed once it spans `every_seconds`, so the AI sees real timestamps it can
    cite instead of thousands of single words.
    """
    lines: list[dict] = []
    current: list[str] = []
    line_start: Optional[float] = None
    for w in words or []:
        try:
            start = float(w["start_s"])
            word = str(w["word"])
        except (KeyError, TypeError, ValueError):
            continue
        if line_start is None:
            line_start = start
        current.append(word)
        if start - line_start >= every_seconds:
            lines.append({"start_s": round(line_start, 1), "text": " ".join(current)})
            current, line_start = [], None
    if current and line_start is not None:
        lines.append({"start_s": round(line_start, 1), "text": " ".join(current)})
    return lines


def chapter_at(chapters: Sequence[dict], seconds: float) -> Optional[dict]:
    """The chapter playing at `seconds`, or None when there are none."""
    current = None
    for chapter in chapters or []:
        if chapter["start_s"] <= seconds:
            current = chapter
        else:
            break
    return current
