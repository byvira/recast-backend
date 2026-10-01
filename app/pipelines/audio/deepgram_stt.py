"""Deepgram speech to text, used only when both Groq Whisper models have failed (a Groq outage or key problem takes out
both, since they share the account). It uses the same Deepgram key as the narration backup. Word timings come back in the
same shape the rest of the pipeline already uses, so a recording is transcribed the same way whichever provider did it.

Every call goes through `track`, so it shows on the LLM health page as a fallback."""
from __future__ import annotations

import logging
import mimetypes
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.shared.llm_health.track import fallback_scope, track

logger = logging.getLogger(__name__)

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = "nova-3"
TIMEOUT_S = 300.0


def available() -> bool:
    return bool(settings.DEEPGRAM_API_KEY)


async def listen(audio_bytes: bytes, filename: str, language: Optional[str], *, utterances: bool = False) -> dict[str, Any]:
    """The raw Deepgram result for one recording. `language=None` asks Deepgram to detect it. Raises on any failure."""
    params: dict[str, Any] = {"model": DEEPGRAM_MODEL, "smart_format": "true", "punctuate": "true"}
    if language:
        params["language"] = language
    else:
        params["detect_language"] = "true"
    if utterances:
        params["utterances"] = "true"
    mime = mimetypes.guess_type(filename)[0] or "audio/mpeg"
    with fallback_scope():
        async with track("deepgram", DEEPGRAM_MODEL, feature="transcription"):
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(
                    DEEPGRAM_LISTEN_URL, params=params, content=audio_bytes,
                    headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}", "Content-Type": mime},
                )
                r.raise_for_status()
                return r.json()


def words_from(result: dict[str, Any]) -> list[tuple[str, float, float]]:
    """(word, start_s, end_s) in order, using the punctuated form when Deepgram gives one."""
    channels = (result.get("results") or {}).get("channels") or [{}]
    alt = ((channels[0].get("alternatives")) or [{}])[0]
    return [
        (w.get("punctuated_word") or w.get("word") or "", float(w.get("start", 0.0)), float(w.get("end", 0.0)))
        for w in alt.get("words") or []
        if (w.get("punctuated_word") or w.get("word"))
    ]


def detected_language(result: dict[str, Any]) -> Optional[str]:
    channels = (result.get("results") or {}).get("channels") or [{}]
    return channels[0].get("detected_language")


def text_and_segments(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The whole text and segment list (start, end, text), from utterances when present and otherwise from the words."""
    utterances = (result.get("results") or {}).get("utterances") or []
    if utterances:
        segs = [{"start": float(u.get("start", 0.0)), "end": float(u.get("end", 0.0)), "text": (u.get("transcript") or "").strip()} for u in utterances]
        return " ".join(s["text"] for s in segs).strip(), segs
    words = words_from(result)
    if not words:
        return "", []
    return " ".join(w for w, _, _ in words), [{"start": words[0][1], "end": words[-1][2], "text": " ".join(w for w, _, _ in words)}]
