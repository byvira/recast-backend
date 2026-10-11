"""A translation cut off by the model's output limit is never narrated as if it were complete."""
from unittest.mock import AsyncMock

from app.api.v1 import audio_assets as audio_module
from app.api.v1.audio_assets import translation_looks_complete


def test_a_half_length_translation_is_rejected_and_a_normal_one_passes():
    source = "A long narration script. " * 40
    assert not translation_looks_complete(source, "A long narration script. " * 10)
    assert translation_looks_complete(source, "Un long script de narration. " * 38)
    assert not translation_looks_complete(source, "")


async def test_a_cut_short_attempt_is_retried_and_the_whole_one_is_kept(monkeypatch):
    source = "Good morning everyone, here is the full story of the week. " * 10
    replies = [
        "Bonjour tout le monde.",  # cut short, never scored
        "Bonjour tout le monde, voici toute l'histoire de la semaine. " * 10,
        "SCORE: 0.95 REASON: natural",
    ]
    monkeypatch.setattr(audio_module, "call_llm", AsyncMock(side_effect=replies))
    text, score = await audio_module._translate_with_quality_gate(source, "French")
    assert text.startswith("Bonjour tout le monde, voici") and score == 0.95


async def test_when_every_attempt_is_cut_short_there_is_no_translation_to_narrate(monkeypatch):
    source = "Good morning everyone, here is the full story of the week. " * 10
    monkeypatch.setattr(audio_module, "call_llm", AsyncMock(return_value="Bonjour."))
    text, score = await audio_module._translate_with_quality_gate(source, "French")
    assert text == ""
