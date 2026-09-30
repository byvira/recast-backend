"""Spoken length: how long a script takes to read, and what length was asked for.

Pure functions, no network. Used by audio generation to
- show the member the length a script will run before any provider quota is spent,
- pick up a length written in the script or prompt ("a 2 minute intro"),
- keep the target between a sensible minimum and maximum,
- work out the real length of a finished recording from its word timings.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

MIN_TARGET_SECONDS = 15
MAX_TARGET_SECONDS = 600  # 10 minutes per generation; longer content is split into parts
DEFAULT_WORDS_PER_MINUTE = 150  # ordinary spoken pace, used when no pace is chosen
MIN_WORDS_PER_MINUTE = 100
MAX_WORDS_PER_MINUTE = 200

PRESETS_SECONDS = (30, 60, 120, 300, 600)

_WORD_RE = re.compile(r"\S+")

# "2 minute", "2-minute", "2 min", "90 seconds", "30s", "1.5 minutes", "1 hour"
_LENGTH_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*[- ]?\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b(?!\w)",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}
# A number followed by a unit letter is only a duration when a cue word is near it.
_CUE_RE = re.compile(r"\b(long|length|duration|runtime|run time|about|around|approximately|lasting|last|episode|intro|outro|ad|spot|clip|podcast|narration|voiceover|script)\b", re.IGNORECASE)


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def clamp_words_per_minute(wpm: Optional[float]) -> int:
    if wpm is None or wpm != wpm:  # None or NaN
        return DEFAULT_WORDS_PER_MINUTE
    return int(min(MAX_WORDS_PER_MINUTE, max(MIN_WORDS_PER_MINUTE, round(wpm))))


def estimate_seconds(text: str, wpm: Optional[float] = None) -> float:
    """How long `text` takes to read aloud at `wpm` words a minute."""
    words = count_words(text)
    return round(words / clamp_words_per_minute(wpm) * 60, 1)


def clamp_target_seconds(seconds: float) -> int:
    return int(min(MAX_TARGET_SECONDS, max(MIN_TARGET_SECONDS, round(seconds))))


def words_for_seconds(seconds: float, wpm: Optional[float] = None) -> int:
    """How many words fill `seconds` at `wpm`."""
    return max(1, round(seconds / 60 * clamp_words_per_minute(wpm)))


def detect_requested_seconds(text: str) -> Optional[int]:
    """A length the member wrote in a script or prompt, in seconds, or None.

    Reads things like "a 2 minute intro", "30 second ad", "about 90 seconds" and
    "1.5 minutes". A bare number with a one-letter unit ("5 m", "30 s") counts only
    when a word like "long", "intro" or "episode" is close by, so ordinary text
    such as "section 5 s" or "room 2 m" is not mistaken for a length. The first
    length found wins; it is kept inside the allowed minimum and maximum.
    """
    if not text:
        return None
    for match in _LENGTH_RE.finditer(text):
        number = float(match.group(1))
        unit = match.group(2).lower()
        one_letter = len(unit) == 1
        if one_letter:
            window = text[max(0, match.start() - 40): match.end() + 40]
            if not _CUE_RE.search(window):
                continue
        seconds = number * _UNIT_SECONDS[unit[0]]
        if seconds <= 0:
            continue
        return clamp_target_seconds(seconds)
    return None


def fit_assessment(text: str, target_seconds: float, wpm: Optional[float] = None) -> dict:
    """How well a script fits a target length, for the "Expand to fit" / "Trim to fit" hints.

    status is "short", "long" or "ok" (within 15% either way). `words_needed` is the
    word count that would fill the target.
    """
    estimate = estimate_seconds(text, wpm)
    target = float(target_seconds)
    needed = words_for_seconds(target, wpm)
    if target <= 0:
        status = "ok"
    elif estimate < target * 0.85:
        status = "short"
    elif estimate > target * 1.15:
        status = "long"
    else:
        status = "ok"
    return {
        "status": status,
        "estimated_seconds": estimate,
        "target_seconds": target,
        "words": count_words(text),
        "words_needed": needed,
    }


def duration_from_words(words: Sequence[object]) -> Optional[float]:
    """The real length of a recording: the end of its last timed word.

    `words` are transcript entries with an `end_s` attribute or key. Returns None
    when there are no timings (for example audio from a provider that gives none),
    never a guess.
    """
    ends: list[float] = []
    for w in words or []:
        end = w.get("end_s") if isinstance(w, dict) else getattr(w, "end_s", None)
        if isinstance(end, (int, float)) and end == end and end >= 0:
            ends.append(float(end))
    return round(max(ends), 2) if ends else None


def audio_duration_seconds(data: bytes) -> Optional[float]:
    """Length of an MP3 or WAV held in memory, read from the file itself, or None if unreadable."""
    try:
        from io import BytesIO

        import soundfile as sf

        info = sf.info(BytesIO(data))
        return round(float(info.duration), 2) if info.duration and info.duration > 0 else None
    except Exception:  # noqa: BLE001 - an odd file is "unknown", never an error
        return None


def trim_to_seconds(text: str, max_seconds: float, wpm: Optional[float] = None) -> str:
    """The script cut at a sentence end so it speaks in no more than `max_seconds`.
    Never cuts mid-sentence; if even the first sentence is too long it is cut at a word.
    Text that already fits is returned unchanged."""
    text = (text or "").strip()
    if not text or estimate_seconds(text, wpm) <= max_seconds:
        return text
    budget = max(1, words_for_seconds(max_seconds, wpm))
    sentences = re.split(r"(?<=[.!?।。])\s+", text)
    kept: list[str] = []
    used = 0
    for sentence in sentences:
        n = count_words(sentence)
        if used + n > budget:
            break
        kept.append(sentence)
        used += n
    if kept:
        return " ".join(kept).strip()
    return " ".join(_WORD_RE.findall(text)[:budget])
