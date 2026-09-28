"""Tests that a recording's real per-word timing actually reaches the saved
AudioAsset — not just the empty-transcript fallback path every other test
exercises (`stubs`'s default `timed_words=None`), which is all the earlier
P2-1 tests ever proved. This is what caught the missing `TranscriptWord`
import that every one of those tests silently walked around.
"""
import pytest

from app.pipelines.media.tts_generation import TranscriptWord as ProviderWord
from tests.test_audio_assets import _generate, _h, _setup, stubs, translation  # noqa: F401 — fixture reuse


def _words(*specs: tuple[str, float, float]) -> list[ProviderWord]:
    return [ProviderWord(word=w, start_s=s, end_s=e) for w, s, e in specs]


async def test_generate_saves_the_real_timed_words_the_provider_returned(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    stubs["timed_words"] = _words(("Hello", 0.0, 0.4), ("world.", 0.4, 0.9))

    res = await _generate(client, ws_id, brand_id, script="Hello world.")
    assert res.status_code == 201, res.text
    transcript = res.json()["transcript"]
    assert [w["word"] for w in transcript] == ["Hello", "world."]
    assert transcript[1]["start_s"] == 0.4 and transcript[1]["end_s"] == 0.9


async def test_dialogue_offsets_each_turns_real_words_by_the_turns_own_position(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    # Each turn's stub returns the same two words riding on the same fixed
    # 0.4s stub clip (`_wav(0.4)`, from the `stubs` fixture). The endpoint
    # must offset turn 2's words by turn 1's real decoded audio length
    # (0.4s), not by the sum of turn 1's own word timings (which end at 0.5s)
    # — those two numbers are deliberately different here to prove it.
    stubs["timed_words"] = _words(("Hi", 0.0, 0.2), ("there.", 0.2, 0.5))
    res = await client.post(
        "/api/v1/audio-assets/dialogue",
        json={
            "title": "Ep", "brand_id": brand_id,
            "turns": [{"speaker": "Host", "text": "Hi there."}, {"speaker": "Guest", "text": "Hi there."}],
        },
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    transcript = res.json()["transcript"]
    assert len(transcript) == 4
    assert transcript[0]["speaker"] == "Host" and transcript[2]["speaker"] == "Guest"
    # Turn 2's words are offset by turn 1's real 0.4s clip, not its 0.5s of
    # word timing.
    assert transcript[2]["start_s"] == pytest.approx(0.4)
    assert transcript[3]["start_s"] == pytest.approx(0.6)


async def test_localize_saves_the_target_languages_real_timed_words(signup_user, stubs, translation):
    client, _, ws_id, brand_id = await _setup(signup_user)
    source = (await _generate(client, ws_id, brand_id, script="Good morning.")).json()
    translation.extend(["Bonjour.", "SCORE: 0.95 REASON: natural"])
    stubs["timed_words"] = _words(("Bonjour.", 0.0, 0.6))

    res = await client.post(
        f"/api/v1/audio-assets/{source['id']}/localize", json={"target_language": "fr"}, headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    transcript = res.json()["transcript"]
    assert transcript == [{"word": "Bonjour.", "start_s": 0.0, "end_s": 0.6, "speaker": None}]
