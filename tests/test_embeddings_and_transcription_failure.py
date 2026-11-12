"""Embeddings fall back to other vendors and say which model made the vectors; a transcription that every provider failed is a retryable error when
asked for strictly, while a recording with no speech stays an empty result. No network."""
import asyncio

import pytest

from app.core.failures import AIUnavailable
from app.pipelines.audio import deepgram_stt, transcriber, voxtral_stt
from app.shared import embeddings
from app.shared.llm_health import probe


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    async def no_refresh(*a, **k):
        return None

    monkeypatch.setattr(probe, "refresh", no_refresh)
    probe._down.clear()
    yield
    probe._down.clear()


def _keys(monkeypatch, gemini=True, mistral=True, cloudflare=True):
    monkeypatch.setattr(embeddings.settings, "GEMINI_API_KEY", "k" if gemini else "", raising=False)
    monkeypatch.setattr(embeddings.settings, "MISTRAL_API_KEY", "k" if mistral else "", raising=False)
    monkeypatch.setattr(embeddings.settings, "CLOUDFLARE_API_TOKEN", "k" if cloudflare else "", raising=False)
    monkeypatch.setattr(embeddings.settings, "CLOUDFLARE_ACCOUNT_ID", "a" if cloudflare else "", raising=False)


def test_gemini_answers_first_and_the_space_names_it(monkeypatch):
    import app.shared.llm as llm

    _keys(monkeypatch)

    async def gemini(text, **k):
        return [1.0, 0.0]

    monkeypatch.setattr(llm, "embed_text", gemini)
    vectors, space = run(embeddings.embed_with_fallback(["a", "b"]))
    assert vectors == [[1.0, 0.0], [1.0, 0.0]] and space.startswith("gemini:")


def test_when_gemini_gives_nothing_mistral_answers_and_then_cloudflare(monkeypatch):
    import app.shared.llm as llm

    _keys(monkeypatch)

    async def nothing(text, **k):
        return []

    async def mistral(texts):
        return [[3.0, 4.0] for _ in texts]

    async def cloudflare(texts):
        return [[0.0, 2.0] for _ in texts]

    monkeypatch.setattr(llm, "embed_text", nothing)
    monkeypatch.setattr(embeddings, "_mistral", mistral)
    monkeypatch.setattr(embeddings, "_cloudflare", cloudflare)
    vectors, space = run(embeddings.embed_with_fallback(["a"]))
    assert space.startswith("mistral:") and vectors == [[0.6, 0.8]]  # unit length

    async def broken(texts):
        raise RuntimeError("down")

    monkeypatch.setattr(embeddings, "_mistral", broken)
    vectors, space = run(embeddings.embed_with_fallback(["a"]))
    assert space.startswith("cloudflare:") and vectors == [[0.0, 1.0]]


def test_a_step_the_check_found_down_is_skipped_unless_every_step_is(monkeypatch):
    import app.shared.llm as llm

    _keys(monkeypatch)
    calls: list[str] = []

    async def nothing(text, **k):
        return []

    async def mistral(texts):
        calls.append("mistral")
        return [[1.0, 0.0]]

    async def cloudflare(texts):
        calls.append("cloudflare")
        return [[0.0, 1.0]]

    monkeypatch.setattr(llm, "embed_text", nothing)
    monkeypatch.setattr(embeddings, "_mistral", mistral)
    monkeypatch.setattr(embeddings, "_cloudflare", cloudflare)
    probe._down[f"embeddings:mistral:{embeddings.settings.MISTRAL_EMBED_MODEL}"] = "retired"
    _, space = run(embeddings.embed_with_fallback(["a"]))
    assert space.startswith("cloudflare:") and calls == ["cloudflare"]


def test_every_provider_failing_or_an_empty_text_gives_no_vectors_and_never_raises(monkeypatch):
    import app.shared.llm as llm

    _keys(monkeypatch)

    async def nothing(text, **k):
        return []

    async def broken(texts):
        raise RuntimeError("down")

    monkeypatch.setattr(llm, "embed_text", nothing)
    monkeypatch.setattr(embeddings, "_mistral", broken)
    monkeypatch.setattr(embeddings, "_cloudflare", broken)
    assert run(embeddings.embed_with_fallback(["a"])) == ([], "")
    assert run(embeddings.embed_with_fallback(["  "])) == ([], "")


def test_best_match_ignores_vectors_from_another_space():
    assert embeddings.best_match([1.0, 0.0], [[0.0, 1.0], [0.9, 0.1]]) == 1
    assert embeddings.best_match([1.0, 0.0], [[1.0, 0.0, 0.0]]) is None
    assert embeddings.best_match([], [[1.0]]) is None


# ── transcription ────────────────────────────────────────────────────────────

def _everything_fails(monkeypatch):
    async def fit(audio, name):
        return audio, name

    class Broken:
        class audio:
            class transcriptions:
                @staticmethod
                async def create(**k):
                    raise RuntimeError("groq down")

    monkeypatch.setattr(transcriber, "_fit_for_whisper", fit)
    monkeypatch.setattr(transcriber, "get_groq_client", lambda: Broken)
    monkeypatch.setattr(deepgram_stt, "available", lambda: False)
    monkeypatch.setattr(voxtral_stt, "available", lambda: False)


def test_every_speech_service_failing_is_a_retryable_error_when_strict_and_empty_otherwise(monkeypatch):
    _everything_fails(monkeypatch)
    assert run(transcriber.transcribe_audio_detailed(b"x", "a.mp3")) == ([], None)
    with pytest.raises(AIUnavailable) as caught:
        run(transcriber.transcribe_audio_detailed(b"x", "a.mp3", strict=True))
    assert caught.value.status_code == 503 and caught.value.code == "transcription_busy" and caught.value.retry_after == 60


def test_voxtral_is_the_last_backup_and_its_words_keep_their_times(monkeypatch):
    _everything_fails(monkeypatch)
    monkeypatch.setattr(voxtral_stt, "available", lambda: True)

    async def listen(audio, filename, language):
        return {"language": "en", "segments": [{"text": " Hello", "start": 0.1, "end": 0.4}, {"text": " world.", "start": 0.4, "end": 0.9}]}

    monkeypatch.setattr(voxtral_stt, "listen", listen)
    words, language = run(transcriber.transcribe_audio_detailed(b"x", "a.mp3", strict=True))
    assert [(w.word, w.start_s, w.end_s) for w in words] == [("Hello", 0.1, 0.4), ("world.", 0.4, 0.9)] and language == "en"
