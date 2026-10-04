"""Real understanding of an uploaded video/audio recording.

Until this existed nothing in the app could "read" an uploaded video: the
YouTube description came from the post text, chapters came from a chip prompt
that told the model to "space sections approximately evenly", and there were
no captions. Now the recording is transcribed (Groq Whisper, word-level) and
chapters are chosen from real transcript positions.

No ffmpeg (a standing project rule): Cloudinary serves the audio track of any
uploaded video as an MP3 when the URL's extension is swapped, and that small
file is what Whisper transcribes.

Chapters are never invented. The model may only cite a time that appears in
the transcript, every cited time is snapped to a real segment start (and
rejected if it isn't within a few seconds of one), and anything that doesn't
meet YouTube's own rules (3+ chapters, first at 0:00, 10s apart) is dropped
rather than padded.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.models.media import (
    MediaAnalysisStatus,
    MediaChapter,
    MediaTranscriptWord,
)
from app.pipelines.audio.transcriber import transcribe_audio_detailed
from app.prompts.registry import load_prompt
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)

# Groq's Whisper accepts up to 25MB on the free tier.
MAX_TRANSCRIBE_BYTES = 24 * 1024 * 1024

MIN_CHAPTERS = 3            # YouTube ignores chapters below this
MAX_CHAPTERS = 10
MIN_CHAPTER_GAP_S = 10.0    # YouTube requires each chapter to be >= 10s
MIN_VIDEO_FOR_CHAPTERS_S = 30.0
SNAP_TOLERANCE_S = 5.0      # a cited time this far from any real segment start is rejected
FIRST_CHAPTER_MAX_START_S = 15.0
MAX_LLM_TRANSCRIPT_CHARS = 24000
MAX_TITLE_CHARS = 60


@dataclass
class Segment:
    start_s: float
    end_s: float
    text: str


# ── timestamps ───────────────────────────────────────────────────────────────

def format_timestamp(seconds: float) -> str:
    """0 -> "0:00", 75 -> "1:15", 3725 -> "1:02:05" (the form YouTube parses)."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_timestamp(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


# ── Cloudinary URL derivatives ───────────────────────────────────────────────

_EXT = re.compile(r"\.[A-Za-z0-9]{2,5}(?=($|\?))")


def audio_track_url(url: str) -> str:
    """The audio track of a Cloudinary-hosted video/audio as an MP3."""
    return _EXT.sub(".mp3", url, count=1) if "/upload/" in url else url


def poster_url_for(url: str) -> Optional[str]:
    """A still frame (first second) of a Cloudinary-hosted video."""
    if "/video/upload/" not in url:
        return None
    framed = url.replace("/video/upload/", "/video/upload/so_1,w_960,c_limit/", 1)
    return _EXT.sub(".jpg", framed, count=1)


# ── transcript shaping ───────────────────────────────────────────────────────

def words_to_segments(
    words: list[MediaTranscriptWord], max_span_s: float = 8.0, max_chars: int = 160,
) -> list[Segment]:
    """Group words into readable, sentence-ish segments, each keeping the real
    start time of its first word."""
    segments: list[Segment] = []
    cur: list[MediaTranscriptWord] = []

    def flush() -> None:
        if cur:
            text = " ".join(w.word.strip() for w in cur).strip()
            if text:
                segments.append(Segment(cur[0].start_s, cur[-1].end_s, text))
            cur.clear()

    for w in words:
        if not w.word.strip():
            continue
        if cur:
            span = w.end_s - cur[0].start_s
            chars = sum(len(x.word) + 1 for x in cur)
            gap = w.start_s - cur[-1].end_s
            if span > max_span_s or chars > max_chars or gap > 1.2:
                flush()
        cur.append(w)
        if w.word.strip()[-1] in ".?!。！？" and (cur[-1].end_s - cur[0].start_s) >= 3.0:
            flush()
    flush()
    return segments


def transcript_text(words: list[MediaTranscriptWord]) -> str:
    return " ".join(w.word.strip() for w in words if w.word.strip())


# ── chapters ─────────────────────────────────────────────────────────────────

_TS_PREFIX = re.compile(r"^\s*\[?\(?\d{1,2}:\d{2}(?::\d{2})?\)?\]?\s*[-–:.]?\s*")


def _clean_title(title: Any) -> str:
    if not isinstance(title, str):
        return ""
    t = _TS_PREFIX.sub("", title).strip().strip("\"'`“”‘’").strip(" .:-")
    return t[:MAX_TITLE_CHARS].strip()


def validate_chapters(
    raw: Any, segments: list[Segment], duration_s: Optional[float],
) -> list[MediaChapter]:
    """Turn model output into chapters that are guaranteed real: every start
    is a genuine segment start, the list obeys YouTube's chapter rules, and
    anything that can't is dropped (an honest empty list, never padding)."""
    if not segments:
        return []
    total = duration_s if duration_s else segments[-1].end_s
    if total < MIN_VIDEO_FOR_CHAPTERS_S:
        return []
    if not isinstance(raw, list):
        return []

    starts = [s.start_s for s in segments]
    picked: dict[float, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = parse_timestamp(item.get("start"))
        title = _clean_title(item.get("title"))
        if t is None or not title:
            continue
        snapped = min(starts, key=lambda s: abs(s - t))
        if abs(snapped - t) > SNAP_TOLERANCE_S:
            continue  # the model cited a moment that isn't in the transcript
        picked.setdefault(snapped, title)

    ordered = sorted(picked.items())
    if not ordered:
        return []

    # YouTube requires the first chapter at 0:00. Only allow that when the
    # speech really begins near the start (the opening chapter then honestly
    # covers it); otherwise there is no valid chapter list to give.
    first_start, first_title = ordered[0]
    if first_start > FIRST_CHAPTER_MAX_START_S:
        return []
    ordered[0] = (0.0, first_title)

    kept: list[tuple[float, str]] = []
    for start, title in ordered:
        if kept and start - kept[-1][0] < MIN_CHAPTER_GAP_S:
            continue
        kept.append((start, title))
    kept = kept[:MAX_CHAPTERS]

    if len(kept) < MIN_CHAPTERS:
        return []
    return [MediaChapter(start_s=s, title=t) for s, t in kept]


async def generate_chapters(
    words: list[MediaTranscriptWord], language: Optional[str], duration_s: Optional[float],
) -> list[MediaChapter]:
    segments = words_to_segments(words)
    total = duration_s if duration_s else (segments[-1].end_s if segments else 0.0)
    if not segments or total < MIN_VIDEO_FOR_CHAPTERS_S:
        return []

    lines: list[str] = []
    used = 0
    for seg in segments:
        line = f"[{format_timestamp(seg.start_s)}] {seg.text}"
        if used + len(line) > MAX_LLM_TRANSCRIPT_CHARS:
            break
        lines.append(line)
        used += len(line) + 1

    prompt = load_prompt(
        "media/video/chapters",
        duration_label=format_timestamp(total),
        language=language or "the spoken language",
        min_chapters=MIN_CHAPTERS,
        max_chapters=min(MAX_CHAPTERS, max(MIN_CHAPTERS, int(total // 60) + MIN_CHAPTERS)),
        transcript_lines="\n".join(lines),
    )
    try:
        result = await call_llm_structured(prompt, max_tokens=900)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chapter generation failed, leaving chapters empty: %s", exc)
        return []
    return validate_chapters((result or {}).get("chapters"), segments, total)


def chapters_block(chapters: list[MediaChapter]) -> str:
    """The exact form YouTube turns into chapters: one "m:ss Title" per line."""
    return "\n".join(f"{format_timestamp(c.start_s)} {c.title}" for c in chapters)


# ── the whole analysis ───────────────────────────────────────────────────────

async def analyze_media(asset: dict) -> dict:
    """Transcribe a video/audio MediaAsset document and build its chapters.
    Returns the fields to $set on the media_assets document. Never raises:
    a failure comes back as analysis_status="failed" with a plain reason."""
    url = asset.get("url") or ""
    duration_s: Optional[float] = asset.get("duration_s")
    update: dict[str, Any] = {}
    if asset.get("kind") == "video" and not asset.get("poster_url"):
        update["poster_url"] = poster_url_for(url)

    def failed(reason: str) -> dict:
        return {**update, "analysis_status": MediaAnalysisStatus.FAILED.value, "analysis_error": reason}

    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(audio_track_url(url))
            resp.raise_for_status()
            audio = resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("Couldn't fetch the audio track for media %s: %s", asset.get("id"), exc)
        return failed("The recording's audio couldn't be read. Try again in a moment.")

    if len(audio) > MAX_TRANSCRIBE_BYTES:
        mb = MAX_TRANSCRIBE_BYTES // (1024 * 1024)
        return failed(f"This recording is too long to transcribe automatically (audio over {mb}MB).")

    words, language = await transcribe_audio_detailed(audio, "audio.mp3", None)
    if not words:
        return failed("No speech could be transcribed from this recording.")

    media_words = [MediaTranscriptWord(word=w.word, start_s=w.start_s, end_s=w.end_s) for w in words]
    from app.agents.content_guard.media import speech_problem, transcript_text

    problem = speech_problem(transcript_text(media_words), "video" if asset.get("kind") == "video" else "recording")
    if problem:
        return failed(problem[0])
    duration = duration_s or media_words[-1].end_s
    chapters = await generate_chapters(media_words, language, duration)

    return {
        **update,
        "analysis_status": MediaAnalysisStatus.DONE.value,
        "analysis_error": None,
        "transcript": [w.model_dump() for w in media_words],
        "transcript_language": language,
        "chapters": [c.model_dump() for c in chapters],
        "duration_s": duration,
    }
