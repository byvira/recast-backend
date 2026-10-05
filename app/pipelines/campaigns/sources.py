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
