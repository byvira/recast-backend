"""The words spoken in a YouTube video, from its public captions, so a video link can be written from like an article.

Captions the creator wrote are preferred over the ones YouTube makes by itself, English first, then any language. Nothing is
downloaded but the caption text, and no key or sign-in is needed. Any failure (captions switched off, a private video, YouTube
refusing the server) gives None, and the caller falls back to the page's own text.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
PREFERRED = ("en", "en-IN", "en-US", "en-GB")


def video_id(url: str) -> Optional[str]:
    """The video's id from any common YouTube link (watch, youtu.be, shorts, embed, live), or None for anything else."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if host not in _HOSTS:
        return None
    if host.endswith("youtu.be"):
        candidate = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        parts = [p for p in parsed.path.split("/") if p]
        candidate = parts[1] if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v") else ""
    return candidate if _ID.match(candidate or "") else None


def captions_text(vid: str) -> Optional[str]:
    """The spoken words as one block of text, or None when there are no captions this server can read. Blocking: run off the loop."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        transcripts = list(YouTubeTranscriptApi().list(vid))
        if not transcripts:
            return None

        def rank(t) -> tuple[int, int]:
            lang = (getattr(t, "language_code", "") or "")
            return (1 if t.is_generated else 0, 0 if lang in PREFERRED or lang.startswith("en") else 1)

        chosen = sorted(transcripts, key=rank)[0]
        text = " ".join(snippet.text for snippet in chosen.fetch())
        text = " ".join(text.replace("\n", " ").split())
        return text or None
    except Exception as exc:  # noqa: BLE001 - captions are a bonus; the page text is the fallback
        logger.info("No captions for YouTube video %s: %s", vid, type(exc).__name__)
        return None
