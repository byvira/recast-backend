"""Mistral Voxtral speech to text, the last backup when Groq Whisper and Deepgram have both failed. It is a separate vendor and a separate
key from both, so one provider's outage never takes out all three. Word times come back in the same shape the rest of the pipeline uses.

Every call goes through `track`, so it shows on the LLM health page as a fallback."""
from __future__ import annotations

import mimetypes
from typing import Optional

import httpx

from app.core.config import settings
from app.shared.llm_health.track import fallback_scope, track

URL = "https://api.mistral.ai/v1/audio/transcriptions"
TIMEOUT_S = 300.0


def available() -> bool:
    return bool(settings.MISTRAL_API_KEY and settings.VOXTRAL_MODEL)


def words_from(result: dict) -> list[tuple[str, float, float]]:
    """(word, start, end) for each word. With word timing asked for, every segment is one word."""
    out: list[tuple[str, float, float]] = []
    for seg in result.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if text:
            out.append((text, float(seg.get("start") or 0.0), float(seg.get("end") or 0.0)))
    return out


async def listen(audio_bytes: bytes, filename: str, language: Optional[str]) -> dict:
    """The raw Voxtral result for one recording. `language=None` lets it detect the language. Raises on any failure."""
    data = {"model": settings.VOXTRAL_MODEL, "timestamp_granularities": "word"}
    if language:
        data["language"] = language.split("-")[0].split("+")[0].lower()
    mime = mimetypes.guess_type(filename)[0] or "audio/mpeg"
    with fallback_scope():
        async with track("mistral", settings.VOXTRAL_MODEL, feature="transcription"):
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(URL, headers={"Authorization": f"Bearer {settings.MISTRAL_API_KEY}"}, data=data, files={"file": (filename, audio_bytes, mime)})
                r.raise_for_status()
                return r.json()
