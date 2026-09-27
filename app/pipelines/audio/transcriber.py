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

from app.models.audio_asset import TranscriptWord
from app.shared.llm import GroqModel, get_groq_client

logger = logging.getLogger(__name__)


async def transcribe_audio_bytes(audio_bytes: bytes, filename: str, language: str = "en") -> list[TranscriptWord]:
    """Real Groq Whisper call, word-level timestamps. Returns an empty
    list (never raises) on any provider failure — an uploaded file with
    no transcript yet is a real, honest state (matches AudioAsset.
    transcript's own default `[]`), not a reason to fail the whole
    upload."""
    try:
        client = get_groq_client()
        response = await client.audio.transcriptions.create(
            model=GroqModel.WHISPER.value,
            file=(filename, audio_bytes),
            language=language,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )
        words = getattr(response, "words", None) or []
        return [
            TranscriptWord(word=w.word, start_s=w.start, end_s=w.end)
            for w in words
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed, leaving transcript empty: %s", exc)
        return []


async def transcribe(audio_file: bytes) -> str:
    """Plain-text convenience wrapper over transcribe_audio_bytes, kept
    for any caller that only wants the joined text, not per-word
    timestamps."""
    words = await transcribe_audio_bytes(audio_file, filename="audio.mp3")
    return " ".join(w.word for w in words)
