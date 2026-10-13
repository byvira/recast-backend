"""A long script is read in parts and joined, and the limit that decides what is accepted is one number."""
import asyncio

import pytest

from app.core.config import settings
from app.models.audio_asset import TranscriptWord
from app.pipelines.media import fit_script, tts_generation
from app.pipelines.media.speech_chunks import SPEECH_CHUNK_CHARS, split_for_speech


def test_short_script_is_one_part():
    assert split_for_speech("Hello there.") == ["Hello there."]
    assert split_for_speech("   ") == []


def test_parts_stay_under_the_limit_and_keep_every_word():
    sentence = "This is a sentence of a normal length that a speaker would say. "
    script = (sentence * 20 + "\n\n") * 30
    parts = split_for_speech(script, 1900)
    assert len(parts) > 1
    assert all(len(p) <= 1900 for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(script.split())


def test_cuts_land_on_sentence_ends():
    script = " ".join(f"Sentence number {i} ends here." for i in range(400))
    for part in split_for_speech(script, 500)[:-1]:
        assert part.rstrip().endswith(".")


def test_one_endless_sentence_and_an_unbroken_run_are_still_cut():
    endless = " ".join(["word"] * 2000)
    assert all(len(p) <= 300 for p in split_for_speech(endless, 300))
    unbroken = "x" * 1000
    assert [len(p) for p in split_for_speech(unbroken, 300)] == [300, 300, 300, 100]


def test_one_limit_for_narration_and_fitting():
    assert fit_script.MAX_SCRIPT_CHARS == settings.AUDIO_SCRIPT_MAX_CHARS == 30000
    # A 20 minute script (about 3,000 words) is accepted.
    assert 20 * 150 * 6 <= settings.AUDIO_SCRIPT_MAX_CHARS


def test_long_narration_is_joined_with_shifted_word_times(monkeypatch):
    calls: list[str] = []

    async def fake_part(*, text, timed, **_kw):
        calls.append(text)
        word = TranscriptWord(word=f"p{len(calls)}", start_s=0.0, end_s=2.0)
        return tts_generation.SpeechResult(audio=b"A", words=[word] if timed else None)

    monkeypatch.setattr(tts_generation, "_synthesize_part", fake_part)
    from app.pipelines.media import duration

    monkeypatch.setattr(duration, "audio_duration_seconds", lambda data: 2.0)
    script = " ".join(f"Sentence number {i} ends here." for i in range(300))
    assert len(script) > SPEECH_CHUNK_CHARS

    result = asyncio.run(tts_generation._synthesize(
        text=script, voice_settings=object(), lexicon=None, workspace_id="w", user_id="u", timed=True,
    ))
    assert result is not None
    assert result.audio == b"A" * len(calls)
    assert [w.start_s for w in result.words] == [2.0 * i for i in range(len(calls))]
    assert [w.end_s for w in result.words] == [2.0 * (i + 1) for i in range(len(calls))]


def test_a_failed_part_fails_the_whole_narration(monkeypatch):
    seen = {"n": 0}

    async def fake_part(*, text, **_kw):
        seen["n"] += 1
        return None if seen["n"] == 2 else tts_generation.SpeechResult(audio=b"A")

    monkeypatch.setattr(tts_generation, "_synthesize_part", fake_part)
    script = " ".join(f"Sentence number {i} ends here." for i in range(300))
    result = asyncio.run(tts_generation._synthesize(
        text=script, voice_settings=object(), lexicon=None, workspace_id="w", user_id="u", timed=False,
    ))
    assert result is None
    assert seen["n"] == 2
