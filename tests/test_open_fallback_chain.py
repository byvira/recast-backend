"""Streaming, picture understanding and transcription backups. No network and no real keys."""
import asyncio

import httpx

from app.core.config import settings
from app.shared import llm, open_fallbacks as of


def _client_with(handler, monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(of.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler), **k))


def test_a_stream_that_fails_before_any_output_is_answered_by_the_fallback_chain(monkeypatch):
    async def boom(*a, **k):
        raise llm.APIConnectionError(request=httpx.Request("POST", "http://x"))

    async def backup(prompt, system="", model=None):
        return "answer from the backup"

    monkeypatch.setattr(llm, "get_groq_client", lambda: object())
    monkeypatch.setattr(llm, "_groq_create", boom)
    monkeypatch.setattr(llm, "call_llm_fallback", backup)

    async def run():
        return [c async for c in llm.call_llm_stream("hi")]

    assert asyncio.run(run()) == ["answer from the backup"]


def test_a_stream_that_dies_halfway_is_not_stitched_to_another_provider(monkeypatch):
    class Chunk:
        def __init__(self, text):
            self.choices = [type("C", (), {"delta": type("D", (), {"content": text})()})()]

    async def stream():
        yield Chunk("half")
        raise llm.APIConnectionError(request=httpx.Request("POST", "http://x"))

    async def create(*a, **k):
        return stream()

    async def backup(*a, **k):
        raise AssertionError("must not be called after output began")

    monkeypatch.setattr(llm, "get_groq_client", lambda: object())
    monkeypatch.setattr(llm, "_groq_create", create)
    monkeypatch.setattr(llm, "call_llm_fallback", backup)

    async def run():
        out = []
        try:
            async for c in llm.call_llm_stream("hi"):
                out.append(c)
        except Exception as exc:  # noqa: BLE001
            return out, getattr(exc, "status_code", None)
        return out, None

    assert asyncio.run(run()) == (["half"], 503)


def test_vision_falls_back_to_the_next_free_model_and_sends_the_image(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "k")
    seen = []

    def handler(req):
        import json

        body = json.loads(req.content)
        seen.append(body["model"])
        assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        if len(seen) == 1:
            return httpx.Response(429, json={"error": "busy"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "a blue square"}}]})

    _client_with(handler, monkeypatch)
    assert asyncio.run(of.open_vision_fallback("what is this", b"\xff\xd8abc")) == "a blue square"
    assert seen == [settings.OPENROUTER_VISION_MODEL, settings.OPENROUTER_VISION_MODEL_2]
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    assert asyncio.run(of.open_vision_fallback("what is this", b"x")) is None


def test_transcription_retries_on_the_turbo_model_when_the_first_one_fails(monkeypatch, tmp_path):
    tried = []

    class Transcriptions:
        async def create(self, *, model, **kw):
            tried.append(model)
            if model == llm.GroqModel.WHISPER.value:
                raise RuntimeError("rate limit")
            return type("R", (), {"text": "hello", "segments": [type("S", (), {"start": 0.0, "end": 1.0, "text": "hello"})()]})()

    fake = type("G", (), {"audio": type("A", (), {"transcriptions": Transcriptions()})()})()
    monkeypatch.setattr(llm, "get_groq_client", lambda: fake)
    f = tmp_path / "a.wav"
    f.write_bytes(b"audio")
    out = asyncio.run(llm.transcribe_audio(str(f)))
    assert out["text"] == "hello" and tried == [llm.GroqModel.WHISPER.value, llm.GroqModel.WHISPER_TURBO.value]
