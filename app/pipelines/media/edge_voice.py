"""Microsoft Edge neural voices (the `edge-tts` package), the last narration backup, for the languages nothing else in the chain reads.

It is an unofficial endpoint with no contract and no promised uptime, so it is contained here:
- Off unless `EDGE_TTS_ENABLED` is switched on, and always the last step.
- Never used for private content: the narration of a member's own footage, or of a transcript of their own recording, is marked `private` by its
  caller and never reaches this module.
- Only a fixed table of voices, each checked by reading its audio back through speech to text (2026-11-12). A language or voice name from a
  request is never passed through; a language not in the table is refused before any call.
- Bounded: at most `MAX_CHARS` of text, `TIMEOUT_S` seconds and `MAX_AUDIO_BYTES` of audio per call. Nothing is written to disk and the text is
  never logged.
Every call goes through `track`, so it shows on the LLM health page as a fallback."""
from __future__ import annotations

import asyncio
from typing import Optional

from app.core.config import settings
from app.shared.llm_health.track import track

MAX_CHARS = 3000
MAX_AUDIO_BYTES = 25 * 1024 * 1024
TIMEOUT_S = 60.0

#: language code -> (language name, voice). Each voice was read back through Whisper at 0.76 to 1.0. Vietnamese and Malayalam are left out: no
#: audio came back for Vietnamese, and Malayalam could not be confirmed (speech to text read it as another language).
VOICES: dict[str, tuple[str, str]] = {
    "ko": ("korean", "ko-KR-SunHiNeural"), "ar": ("arabic", "ar-SA-ZariyahNeural"), "zh": ("chinese", "zh-CN-XiaoxiaoNeural"),
    "id": ("indonesian", "id-ID-GadisNeural"), "bn": ("bengali", "bn-IN-TanishaaNeural"), "te": ("telugu", "te-IN-ShrutiNeural"),
    "mr": ("marathi", "mr-IN-AarohiNeural"), "tr": ("turkish", "tr-TR-EmelNeural"), "ru": ("russian", "ru-RU-SvetlanaNeural"),
    "pl": ("polish", "pl-PL-ZofiaNeural"), "sw": ("swahili", "sw-KE-ZuriNeural"), "kn": ("kannada", "kn-IN-SapnaNeural"),
    "tl": ("filipino", "fil-PH-BlessicaNeural"), "pt": ("portuguese", "pt-BR-FranciscaNeural"), "hi": ("hindi", "hi-IN-SwaraNeural"),
    "ta": ("tamil", "ta-IN-PallaviNeural"), "de": ("german", "de-DE-KatjaNeural"), "th": ("thai", "th-TH-NiwatNeural"),
    "ur": ("urdu", "ur-IN-GulNeural"),
}
LANGUAGES = {name for name, _ in VOICES.values()}


def available() -> bool:
    return bool(settings.EDGE_TTS_ENABLED)


def code_for(text: str, language: Optional[str] = None) -> Optional[str]:
    """The language code for this script, or None when it cannot be read: a stated language is trusted over detection, a mixed language
    ("ta+en") or one not in the table is None, and so is a script whose language cannot be told."""
    from app.shared.language import detect_language

    stated = [part.split("-")[0] for part in (language or "").strip().lower().split("+") if part]
    if stated:
        return stated[0] if len(stated) == 1 and stated[0] in VOICES else None
    detected = detect_language(text)
    return detected if detected in VOICES else None


async def speak(text: str, code: str) -> bytes:
    """mp3 speech for `text` in the voice for `code`. Raises on any failure, an unknown code or no audio."""
    if code not in VOICES:
        raise ValueError("No voice for that language.")
    import edge_tts  # imported here so a server without the package still starts

    voice = VOICES[code][1]

    async def collect() -> bytes:
        out = bytearray()
        async for chunk in edge_tts.Communicate((text or "")[:MAX_CHARS], voice).stream():
            if chunk["type"] == "audio":
                out += chunk["data"]
                if len(out) > MAX_AUDIO_BYTES:
                    raise RuntimeError("The speech was longer than allowed.")
        return bytes(out)

    async with track("edge", voice, feature="text_to_speech"):
        audio = await asyncio.wait_for(collect(), timeout=TIMEOUT_S)
    if not audio:
        raise RuntimeError("The speech service returned no audio.")
    return audio
