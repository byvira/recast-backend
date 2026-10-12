"""Real audio transcriber — closes the "audio transcription-in for
uploads" gap named in PROGRESS.md's Deferred list. This file was a
confirmed decoy (`transcribe()` always returned ``""``, no real call
anywhere) — replaced with a real Groq Whisper call, the same real
provider `app.shared.llm.transcribe_audio` already uses for the connected
audio agent, just word-level (not segment-level) so the result matches
`AudioAsset.transcript`'s real `TranscriptWord` shape (word/start_s/
end_s), and taking raw bytes directly rather than requiring a file path.
"""

import asyncio
import contextlib
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from app.models.audio_asset import TranscriptWord
from app.shared.heavy_work import heavy_slot
from app.shared.llm import GroqModel, get_groq_client

logger = logging.getLogger(__name__)


from app.shared.llm_health.track import fallback_scope, track
from app.pipelines.audio import deepgram_stt


_NOT_WORD = re.compile(r"[^\w']+", re.UNICODE)
_EDGE_MARKS = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


def _norm(token: str) -> str:
    return _NOT_WORD.sub("", token or "").casefold()


def restore_punctuation(words: list[TranscriptWord], text: str) -> list[TranscriptWord]:
    """The words with the capital letters and marks the speech service wrote in its full text. Whisper's word list has none
    (it gives "so what do you think" as bare words), but its full text does ("So, what do you think?"). The two are lined up
    word by word; a word that cannot be matched is left as it was, so timings never move."""
    tokens = (text or "").split()
    out: list[TranscriptWord] = []
    at = 0
    for word in words:
        key = _norm(word.word)
        found = None
        if key:
            for k in range(at, min(at + 4, len(tokens))):
                if _norm(tokens[k]) == key:
                    found = k
                    break
        if found is None:
            out.append(word)
            continue
        at = found + 1
        out.append(word.model_copy(update={"word": tokens[found]}))
    return out


def strip_punctuation(words: list[TranscriptWord]) -> list[TranscriptWord]:
    """The words without the marks round them (a bare "hello", not "Hello,")."""
    return [w.model_copy(update={"word": _EDGE_MARKS.sub("", w.word) or w.word}) for w in words]


# The speech service refuses a file over 25 MB, which a long or high-quality recording easily is. Above this size the file is
# first made into a small speech-quality copy (mono, 16 kHz, 32 kbps: about 15 minutes per 4 MB). Word timings are unchanged,
# since only the quality is lowered, not the length.
_WHISPER_MAX_BYTES = 24 * 1024 * 1024


async def _fit_for_whisper(audio_bytes: bytes, filename: str) -> tuple[bytes, str]:
    """The same recording small enough to send, or the original when it is already small or cannot be shrunk."""
    if len(audio_bytes) <= _WHISPER_MAX_BYTES:
        return audio_bytes, filename
    tmp = tempfile.mkdtemp(prefix="recast-stt-")
    try:
        import imageio_ffmpeg

        src, dst = Path(tmp) / "in.audio", Path(tmp) / "out.mp3"
        src.write_bytes(audio_bytes)
        async with heavy_slot():
            proc = await asyncio.create_subprocess_exec(
                imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(dst),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning("Shrinking a long recording for transcription took too long")
                return audio_bytes, filename
        if proc.returncode != 0 or not dst.exists():
            logger.warning("Shrinking a long recording for transcription failed (ffmpeg exit %s)", proc.returncode)
            return audio_bytes, filename
        return dst.read_bytes(), f"{Path(filename).stem}.mp3"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Shrinking a long recording for transcription failed: %s", exc)
        return audio_bytes, filename
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def transcribe_audio_detailed(
    audio_bytes: bytes, filename: str, language: Optional[str] = None, punctuate: Optional[bool] = None,
) -> tuple[list[TranscriptWord], Optional[str]]:
    """Real Groq Whisper call, word-level timestamps, plus the language
    Whisper detected. `language=None` lets Whisper auto-detect — the old
    hard-coded "en" made any non-English recording come back as garbled
    English. Returns ([], None) (never raises) on any provider failure."""
    try:
        audio_bytes, filename = await _fit_for_whisper(audio_bytes, filename)
        client = get_groq_client()
        kwargs: dict = {}
        if language:
            kwargs["language"] = language
        response = None
        failure: Exception | None = None
        for attempt, whisper in enumerate((GroqModel.WHISPER, GroqModel.WHISPER_TURBO)):
            try:
                with fallback_scope() if attempt else contextlib.nullcontext():
                    async with track("groq", whisper.value, feature="transcription"):
                        response = await client.audio.transcriptions.create(
                            model=whisper.value,
                            file=(filename, audio_bytes),
                            response_format="verbose_json",
                            timestamp_granularities=["word"],
                            **kwargs,
                        )
                break
            except Exception as exc:  # noqa: BLE001
                failure = exc
                logger.warning("Whisper (%s) failed: %s", whisper.value, exc)
        if response is None:
            # Both Whisper models failed (same Groq account). Deepgram is the real backup.
            if not deepgram_stt.available():
                raise failure  # type: ignore[misc]
            result = await deepgram_stt.listen(audio_bytes, filename, language, utterances=False)
            fallback_words = [TranscriptWord(word=w, start_s=s, end_s=e) for w, s, e in deepgram_stt.words_from(result)]
            if punctuate is False:
                fallback_words = strip_punctuation(fallback_words)
            return fallback_words, deepgram_stt.detected_language(result) or language
        words = getattr(response, "words", None) or []
        made = [TranscriptWord(word=w.word, start_s=w.start, end_s=w.end) for w in words]
        if punctuate:
            made = restore_punctuation(made, getattr(response, "text", "") or "")
        return made, getattr(response, "language", None) or language
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed, leaving transcript empty: %s", exc)
        return [], None


async def transcribe_audio_bytes(
    audio_bytes: bytes, filename: str, language: Optional[str] = None, punctuate: Optional[bool] = None,
) -> list[TranscriptWord]:
    """Word-level transcript only. `language=None` lets the provider detect it (a forced "en" garbled every non-English
    recording). Returns an empty list (never raises) on
    any provider failure — an uploaded file with no transcript yet is a
    real, honest state (matches AudioAsset.transcript's own default `[]`),
    not a reason to fail the whole upload."""
    words, _ = await transcribe_audio_detailed(audio_bytes, filename, language, punctuate)
    return words


async def transcribe(audio_file: bytes) -> str:
    """Plain-text convenience wrapper over transcribe_audio_bytes, kept
    for any caller that only wants the joined text, not per-word
    timestamps."""
    words = await transcribe_audio_bytes(audio_file, filename="audio.mp3")
    return " ".join(w.word for w in words)
