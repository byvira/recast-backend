"""Turns a recording into the topic text a campaign is built from.

The member records or imports the audio in the Audio pipeline (a pasted YouTube or podcast link is imported there too), which gives
one transcript for the whole recording. This reads that transcript. The writing pipeline reads about 8,000 characters of a source, so a
long recording is condensed into evenly spaced excerpts from the whole of it, not just its opening minutes; the campaign's
day-by-day plan then finds its own angles in that spread of topics.
"""
from __future__ import annotations

import re
from typing import Any

from app.db.mongo import audio_assets

MAX_SOURCE_SECONDS = 60 * 60      # the longest recording a campaign takes as a source, for now
MAX_SOURCE_CHARS = 8000           # what the writing pipeline reads of any source
_WINDOWS = 12


class SourceError(ValueError):
    """A recording that cannot be used as a source, with a plain reason for the member."""


def _sentence_start(text: str, limit: int) -> str:
    """The first `limit` characters of `text`, ended at a sentence break when there is one near the end."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: stop + 1] if stop > limit * 0.5 else cut.rstrip()


def condense(text: str, limit: int = MAX_SOURCE_CHARS, windows: int = _WINDOWS) -> str:
    """The text itself when it fits; otherwise equal-length excerpts taken at even steps through it."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    step = len(text) / windows
    share = max(200, limit // windows - 4)
    parts = []
    for i in range(windows):
        start = int(i * step)
        window = text[start : start + int(step)]
        parts.append(_sentence_start(window, share))
    return "\n\n".join(p for p in parts if p)


async def topic_from_recording(asset_id: str, workspace_id: str) -> tuple[str, dict[str, Any]]:
    """The topic text and a small reference for a recording in this workspace, or a SourceError saying what is wrong."""
    doc = await audio_assets.find_one({"id": (asset_id or "").strip(), "workspace_id": workspace_id})
    if not doc:
        raise SourceError("That recording wasn't found in this workspace.")
    words = doc.get("transcript") or []
    if not words:
        raise SourceError("This recording has no transcript yet. Open it in the Audio pipeline and use Transcribe first.")
    seconds = max((float(w.get("end_s") or 0) for w in words), default=0.0)
    if seconds > MAX_SOURCE_SECONDS:
        minutes = int(round(seconds / 60))
        raise SourceError(f"This recording is about {minutes} minutes long. A campaign can take a source of up to 60 minutes for now.")
    text = " ".join(str(w.get("word") or "") for w in words).strip()
    if not text:
        raise SourceError("This recording's transcript is empty.")
    return condense(text), {"type": "audio_asset", "id": doc["id"], "title": doc.get("title"), "minutes": round(seconds / 60, 1)}


#: The longest part of a source video that goes onto one day's posts.
MAX_CUT_SECONDS = 60.0


async def topic_from_video(media_id: str, workspace_id: str) -> tuple[str, dict[str, Any]]:
    """The topic text and a small reference for a video in this library, or a SourceError saying what is wrong. The video must
    have a finished transcript: its words are what the posts are written from."""
    from app.db.mongo import get_db, media_assets
    from app.video import transcribe

    media = await media_assets.find_one({"id": (media_id or "").strip(), "workspace_id": workspace_id, "kind": "video"})
    if not media:
        raise SourceError("That video was not found in this workspace.")
    doc = await get_db()["video_transcripts"].find_one({"workspace_id": workspace_id, "media_id": media["id"], "status": {"$in": ["done", "partial"]}})
    if not doc:
        raise SourceError("This video has no transcript yet. Open it in the Video pipeline and use Transcribe first.")
    words = await transcribe.all_words(doc["id"])
    text = " ".join(str(w.get("w") or "") for w in words).strip()
    if not text:
        raise SourceError("This video has no speech to write from.")
    seconds = float(media.get("duration_s") or doc.get("duration_s") or 0)
    if seconds > MAX_SOURCE_SECONDS:
        raise SourceError(f"This video is about {int(round(seconds / 60))} minutes long. A campaign can take a source of up to 60 minutes for now.")
    return condense(text), {"type": "video", "id": media["id"], "title": media.get("title") or media.get("filename"),
                            "minutes": round(seconds / 60, 1), "duration_s": round(seconds, 2)}


def window_for_day(duration_s: float, day_index: int, days: int, longest: float = MAX_CUT_SECONDS) -> tuple[float, float]:
    """(start, length) in seconds of the part of a source video that belongs to day `day_index`: the video is shared evenly across
    the days, and a long share is cut to its first `longest` seconds."""
    days = max(1, days)
    share = max(0.0, duration_s) / days
    return round(day_index * share, 3), round(min(share, longest), 3)
