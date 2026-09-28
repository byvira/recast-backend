"""Subtitle/caption files (SRT and WebVTT) built from a real transcript.

Cues come from the word-level timings Whisper returned for the recording
itself, so they line up with the speech — nothing is estimated from the post
text. Broadcast-style limits: at most 2 lines of ~42 characters, a cue never
longer than 6 seconds, and never overlapping the next cue.
"""

from dataclasses import dataclass

from app.models.media import MediaTranscriptWord

MAX_LINE_CHARS = 42
MAX_LINES = 2
MAX_CUE_S = 6.0
MIN_CUE_S = 0.6
BREAK_GAP_S = 0.9  # a pause this long between words always starts a new cue

_SENTENCE_END = ".?!。！？"


@dataclass
class Cue:
    start_s: float
    end_s: float
    text: str


def _wrap(text: str) -> str:
    """Split into at most 2 balanced lines when it doesn't fit on one."""
    if len(text) <= MAX_LINE_CHARS:
        return text
    words = text.split(" ")
    best_i, best_diff = 1, None
    for i in range(1, len(words)):
        diff = abs(len(" ".join(words[:i])) - len(" ".join(words[i:])))
        if best_diff is None or diff < best_diff:
            best_i, best_diff = i, diff
    return " ".join(words[:best_i]) + "\n" + " ".join(words[best_i:])


def build_cues(words: list[MediaTranscriptWord]) -> list[Cue]:
    cues: list[Cue] = []
    cur: list[MediaTranscriptWord] = []
    limit = MAX_LINE_CHARS * MAX_LINES

    def flush() -> None:
        if not cur:
            return
        text = " ".join(w.word.strip() for w in cur).strip()
        if text:
            cues.append(Cue(cur[0].start_s, cur[-1].end_s, _wrap(text)))
        cur.clear()

    for w in words:
        token = w.word.strip()
        if not token:
            continue
        if cur:
            projected = len(" ".join(x.word.strip() for x in cur)) + 1 + len(token)
            gap = w.start_s - cur[-1].end_s
            if projected > limit or (w.end_s - cur[0].start_s) > MAX_CUE_S or gap > BREAK_GAP_S:
                flush()
        cur.append(w)
        if token[-1] in _SENTENCE_END and (cur[-1].end_s - cur[0].start_s) >= 1.5:
            flush()
    flush()

    # Readable timing: a minimum on-screen time, and never overlap the next cue.
    for i, cue in enumerate(cues):
        cue.end_s = max(cue.end_s, cue.start_s + MIN_CUE_S)
        if i + 1 < len(cues):
            cue.end_s = min(cue.end_s, cues[i + 1].start_s)
        if cue.end_s <= cue.start_s:
            cue.end_s = cue.start_s + 0.05
    return cues


def _stamp(seconds: float, sep: str) -> str:
    ms_total = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    blocks = [
        f"{i}\n{_stamp(c.start_s, ',')} --> {_stamp(c.end_s, ',')}\n{c.text}"
        for i, c in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(cues: list[Cue]) -> str:
    blocks = [f"{_stamp(c.start_s, '.')} --> {_stamp(c.end_s, '.')}\n{c.text}" for c in cues]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


# Whisper reports the detected language as a full lowercase name; YouTube's
# caption API needs a BCP-47 code. Only languages we can map with certainty.
_LANGUAGE_CODES = {
    "english": "en", "tamil": "ta", "hindi": "hi", "telugu": "te", "kannada": "kn",
    "malayalam": "ml", "marathi": "mr", "bengali": "bn", "gujarati": "gu", "punjabi": "pa",
    "urdu": "ur", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
    "portuguese": "pt", "dutch": "nl", "russian": "ru", "japanese": "ja", "korean": "ko",
    "chinese": "zh", "arabic": "ar", "turkish": "tr", "indonesian": "id", "vietnamese": "vi",
    "thai": "th", "polish": "pl", "swedish": "sv",
}


def language_code(name: "str | None") -> "str | None":
    """"english" -> "en"; an already-short code passes through; unknown -> None."""
    if not name:
        return None
    n = name.strip().lower()
    if n in _LANGUAGE_CODES:
        return _LANGUAGE_CODES[n]
    return n if 2 <= len(n) <= 3 and n.isalpha() else None
