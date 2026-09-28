"""Real audio transcriber — closes the "audio transcription-in for
uploads" gap named in PROGRESS.md's Deferred list. This file was a
confirmed decoy (`transcribe()` always returned ``""``, no real call
anywhere) — replaced with a real Groq Whisper call, the same real
provider `app.shared.llm.transcribe_audio` already uses for the connected
audio agent, just word-level (not segment-level) so the result matches
`AudioAsset.transcript`'s real `TranscriptWord` shape (word/start_s/
end_s), and taking raw bytes directly rather than requiring a file path.
"""

import logging
from typing import Optional

from app.models.audio_asset import TranscriptWord
from app.shared.llm import GroqModel, get_groq_client

logger = logging.getLogger(__name__)


async def transcribe_audio_detailed(
    audio_bytes: bytes, filename: str, language: Optional[str] = None,
) -> tuple[list[TranscriptWord], Optional[str]]:
    """Real Groq Whisper call, word-level timestamps, plus the language
    Whisper detected. `language=None` lets Whisper auto-detect — the old
    hard-coded "en" made any non-English recording come back as garbled
    English. Returns ([], None) (never raises) on any provider failure."""
    try:
        client = get_groq_client()
        kwargs: dict = {}
        if language:
            kwargs["language"] = language
        response = await client.audio.transcriptions.create(
            model=GroqModel.WHISPER.value,
            file=(filename, audio_bytes),
            response_format="verbose_json",
            timestamp_granularities=["word"],
            **kwargs,
        )
        words = getattr(response, "words", None) or []
        return (
            [TranscriptWord(word=w.word, start_s=w.start, end_s=w.end) for w in words],
            getattr(response, "language", None) or language,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed, leaving transcript empty: %s", exc)
        return [], None


async def transcribe_audio_bytes(audio_bytes: bytes, filename: str, language: Optional[str] = "en") -> list[TranscriptWord]:
    """Word-level transcript only. Returns an empty list (never raises) on
    any provider failure — an uploaded file with no transcript yet is a
    real, honest state (matches AudioAsset.transcript's own default `[]`),
    not a reason to fail the whole upload."""
    words, _ = await transcribe_audio_detailed(audio_bytes, filename, language)
    return words


async def transcribe(audio_file: bytes) -> str:
    """Plain-text convenience wrapper over transcribe_audio_bytes, kept
    for any caller that only wants the joined text, not per-word
    timestamps."""
    words = await transcribe_audio_bytes(audio_file, filename="audio.mp3")
    return " ".join(w.word for w in words)
