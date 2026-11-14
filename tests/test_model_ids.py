"""The Gemini models in use are the current ones (the old speech, text and picture models are being retired), what they cost is in the report, and speech
audio comes out right whether the model answers with raw audio or with a WAV file. No network is called."""
import base64
import contextlib

import httpx

from app.billing import report
from app.core.config import settings
from app.pipelines.media import tts_generation
from app.shared.llm import GeminiModel

RETIRING = ("gemini-2.5-flash-preview-tts", "gemini-2.5-flash-image", "gemini-3.5-flash")


def test_the_models_in_use_are_the_current_ones():
    assert GeminiModel.FLASH.value == "gemini-3.6-flash"
    assert GeminiModel.IMAGE.value == "gemini-3.1-flash-lite-image"
    assert settings.GEMINI_TTS_MODEL == "gemini-3.8-flash-tts"
    for retiring in RETIRING:
        assert retiring not in {m.value for m in GeminiModel} and settings.GEMINI_TTS_MODEL != retiring


def test_each_model_in_use_has_a_price_and_the_old_ones_keep_theirs_for_past_events():
    assert report.TOKEN_RATES[GeminiModel.FLASH.value] == (1.50, 7.50)
    assert report.CALL_RATES[GeminiModel.IMAGE.value] == 0.0336
    assert "gemini-2.5-flash-image" in report.CALL_RATES and "gemini-3.5-flash" in report.TOKEN_RATES


def _speech_reply(audio: bytes, mime: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": mime, "data": base64.b64encode(audio).decode()}}]}}]}


async def _run(monkeypatch, audio: bytes, mime: str) -> list[tuple]:
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/gemini-3.8-flash-tts:generateContent")
        return httpx.Response(200, json=_speech_reply(audio, mime))

    real = httpx.AsyncClient
    monkeypatch.setattr(tts_generation.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(tts_generation, "_to_mp3", lambda data, *args: seen.append((data, args)) or b"mp3")
    monkeypatch.setattr(tts_generation, "track", lambda *a, **k: contextlib.nullcontext_async() if hasattr(contextlib, "nullcontext_async") else _Null())
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    assert await tts_generation._call_gemini_tts(text="Welcome back.") == b"mp3"
    return seen


class _Null:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


async def test_a_wav_answer_is_converted_as_it_is_so_no_click_starts_the_narration(monkeypatch):
    wav = b"RIFF" + b"\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
    (data, args), = await _run(monkeypatch, wav, "audio/wav")
    assert data == wav and args == ()


async def test_a_raw_audio_answer_is_still_read_as_24khz_mono(monkeypatch):
    raw = b"\x01\x00" * 100
    (data, args), = await _run(monkeypatch, raw, "audio/L16;codec=pcm;rate=24000")
    assert data == raw and args == ("-f", "s16le", "-ar", "24000", "-ac", "1")
