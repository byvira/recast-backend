"""Tests for assembling an episode: the mixer on real synthetic audio, the
brand kit, and the endpoints. Assertions measure the mixed audio (order,
length, levels), they don't just check that a function ran.
"""
import io

import numpy as np
import pytest
import soundfile as sf

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import audio_assets, media_assets
from app.pipelines.media.audio_assemble import AssembleError, AssemblePlan, assemble_episode
from tests.test_audio_assets import _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse

SR = 16000


def _wav(signal: np.ndarray, sr: int = SR) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, signal.astype("float32"), sr, format="WAV")
    return buf.getvalue()


def _tone(freq: float, seconds: float, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    return amp * np.sin(2 * np.pi * freq * t)


def _read(data: bytes):
    out, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    return out, sr


def _level(x: np.ndarray, start_s: float, end_s: float, sr: int = SR) -> float:
    return float(np.abs(x[int(start_s * sr): int(end_s * sr)]).mean())


def _freq_level(x: np.ndarray, freq: float, sr: int = SR) -> float:
    spectrum = np.abs(np.fft.rfft(x[:, 0] * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    return float(spectrum[(freqs > freq - 20) & (freqs < freq + 20)].sum())


# ── the mixer ────────────────────────────────────────────────────────────────

def test_intro_voice_outro_are_joined_in_order():
    voice = _wav(_tone(220, 2.0))
    wav, mixed = assemble_episode(
        voice_bytes=voice, plan=AssemblePlan(use_intro=True, use_outro=True),
        intro_bytes=_wav(_tone(600, 1.0)), outro_bytes=_wav(_tone(900, 1.0)),
    )
    out, _ = _read(wav)
    assert len(out) / SR == pytest.approx(4.0, abs=0.1)
    assert mixed["intro"] and mixed["outro"] and mixed["total_seconds"] == pytest.approx(4.0, abs=0.1)
    # 600 Hz first, 220 Hz in the middle, 900 Hz last.
    assert _freq_level(out[: SR], 600) > 5 * _freq_level(out[: SR], 900)
    assert _freq_level(out[int(1.2 * SR): int(2.8 * SR)], 220) > 5 * _freq_level(out[int(1.2 * SR): int(2.8 * SR)], 600)
    assert _freq_level(out[-SR:], 900) > 5 * _freq_level(out[-SR:], 600)


def test_the_sponsor_read_lands_at_the_chosen_second():
    voice = _wav(_tone(220, 4.0))
    wav, mixed = assemble_episode(
        voice_bytes=voice, plan=AssemblePlan(sponsor_at_s=2.0), sponsor_bytes=_wav(_tone(700, 1.0)),
    )
    out, _ = _read(wav)
    assert len(out) / SR == pytest.approx(5.0, abs=0.1)
    assert mixed["sponsor_at_s"] == 2.0 and mixed["sponsor_seconds"] == pytest.approx(1.0, abs=0.05)
    assert _freq_level(out[int(2.2 * SR): int(2.8 * SR)], 700) > 5 * _freq_level(out[int(2.2 * SR): int(2.8 * SR)], 220)
    assert _freq_level(out[int(0.2 * SR): int(1.8 * SR)], 220) > 5 * _freq_level(out[int(0.2 * SR): int(1.8 * SR)], 700)
    assert _freq_level(out[int(3.4 * SR): int(4.8 * SR)], 220) > 5 * _freq_level(out[int(3.4 * SR): int(4.8 * SR)], 700)


def test_music_sits_under_the_voice_and_dips_while_it_speaks():
    # 3s of speech, 3s of silence, all with a music bed underneath.
    voice = np.concatenate([_tone(220, 3.0, 0.4), np.zeros(3 * SR)])
    bed = _wav(_tone(900, 2.0, 0.5))
    wav, mixed = assemble_episode(
        voice_bytes=_wav(voice), plan=AssemblePlan(music_bed_id="b", music_level_db=-10, ducking_db=-20), bed_bytes=bed,
    )
    out, _ = _read(wav)
    assert mixed["music_level_db"] == -10 and mixed["ducking_db"] == -20
    music_while_speaking = _freq_level(out[int(1.0 * SR): int(2.5 * SR)], 900)
    music_in_the_gap = _freq_level(out[int(4.0 * SR): int(5.5 * SR)], 900)
    # Same bed, but much quieter under speech than in the gap.
    assert music_in_the_gap > 3 * music_while_speaking
    # The voice itself is untouched by the music.
    assert _freq_level(out[int(0.5 * SR): int(2.5 * SR)], 220) > 0.8 * _freq_level(voice[int(0.5 * SR): int(2.5 * SR), None], 220)


def test_a_short_bed_loops_to_cover_the_whole_voice():
    voice = _wav(_tone(220, 6.0, 0.4))
    out, _ = _read(assemble_episode(
        voice_bytes=voice, plan=AssemblePlan(music_bed_id="b", music_level_db=-8, ducking_db=0),
        bed_bytes=_wav(_tone(900, 1.0, 0.5)),
    )[0])
    assert _freq_level(out[int(4.0 * SR): int(5.5 * SR)], 900) > 0  # still playing late in the episode
    assert _freq_level(out[int(4.0 * SR): int(5.5 * SR)], 900) > 0.5 * _freq_level(out[int(1.0 * SR): int(2.5 * SR)], 900)


def test_other_sample_rates_and_channel_counts_are_matched_to_the_voice():
    voice = _wav(_tone(220, 2.0))
    intro_44k_stereo = _wav(np.stack([_tone(600, 1.0, sr=44100)] * 2, axis=1), sr=44100)
    wav, _ = assemble_episode(voice_bytes=voice, plan=AssemblePlan(use_intro=True), intro_bytes=intro_44k_stereo)
    out, sr = _read(wav)
    assert sr == SR and out.shape[1] == 1
    assert len(out) / SR == pytest.approx(3.0, abs=0.1)
    assert _freq_level(out[: SR], 600) > 5 * _freq_level(out[: SR], 220)


def test_a_stereo_voice_stays_stereo():
    voice = _wav(np.stack([_tone(220, 2.0), _tone(330, 2.0)], axis=1))
    out, _ = _read(assemble_episode(voice_bytes=voice, plan=AssemblePlan(use_outro=True), outro_bytes=_wav(_tone(600, 1.0)))[0])
    assert out.shape[1] == 2


def test_the_result_is_never_clipped():
    loud = _wav(_tone(220, 2.0, 0.95))
    out, _ = _read(assemble_episode(
        voice_bytes=loud, plan=AssemblePlan(music_bed_id="b", music_level_db=-3, ducking_db=0),
        bed_bytes=_wav(_tone(900, 2.0, 0.95)),
    )[0])
    # 16-bit storage rounds the 0.99 ceiling by a hair, so allow one step.
    assert np.abs(out).max() <= 0.995
    assert np.abs(out).max() < 1.0


def test_missing_pieces_and_bad_files_are_refused_with_a_reason():
    voice = _wav(_tone(220, 2.0))
    with pytest.raises(AssembleError, match="intro"):
        assemble_episode(voice_bytes=voice, plan=AssemblePlan(use_intro=True))
    with pytest.raises(AssembleError, match="sponsor"):
        assemble_episode(voice_bytes=voice, plan=AssemblePlan(sponsor_at_s=1.0))
    with pytest.raises(AssembleError, match="after the recording ends"):
        assemble_episode(voice_bytes=voice, plan=AssemblePlan(sponsor_at_s=30.0), sponsor_bytes=_wav(_tone(700, 1.0)))
    with pytest.raises(AssembleError, match="can be mixed"):
        assemble_episode(voice_bytes=voice, plan=AssemblePlan(use_outro=True), outro_bytes=b"junk")


def test_plan_values_are_range_checked():
    from pydantic import ValidationError

    for bad in ({"music_level_db": 5}, {"ducking_db": -90}, {"sponsor_at_s": -1}):
        with pytest.raises(ValidationError):
            AssemblePlan(**bad)


# ── the kit ──────────────────────────────────────────────────────────────────

async def _media(ws_id: str, kind: str = "audio") -> str:
    from datetime import datetime, timezone
    from uuid import uuid4

    mid = uuid4().hex
    await media_assets.insert_one({
        "id": mid, "workspace_id": ws_id, "kind": kind, "url": f"https://res.example.com/{mid}.wav",
        "mime_type": "audio/wav", "source": "uploaded", "created_by": "u", "created_at": datetime.now(timezone.utc),
    })
    return mid


async def test_the_kit_starts_empty_and_saves_only_what_is_sent(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    empty = (await client.get(f"/api/v1/audio-assets/kit/{brand_id}", headers=_h(ws_id))).json()
    assert empty["intro"] is None and empty["music_beds"] == [] and empty["sponsor_name"] == ""

    intro = await _media(ws_id)
    saved = await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}",
        json={"intro": {"media_id": intro, "name": "Sting.wav"}, "sponsor_name": "Zendly", "sponsor_script": "Brought to you by Zendly."},
        headers=_h(ws_id),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["intro"]["url"].endswith(f"{intro}.wav")

    # A later update that only sets the outro leaves the intro and sponsor alone.
    outro = await _media(ws_id)
    again = (await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}", json={"outro": {"media_id": outro, "name": "Out.wav"}}, headers=_h(ws_id),
    )).json()
    assert again["intro"]["name"] == "Sting.wav" and again["outro"]["name"] == "Out.wav"
    assert again["sponsor_name"] == "Zendly"

    # Sending a clip as null clears it.
    cleared = (await client.put(f"/api/v1/audio-assets/kit/{brand_id}", json={"intro": None}, headers=_h(ws_id))).json()
    assert cleared["intro"] is None and cleared["outro"]["name"] == "Out.wav"


async def test_kit_clips_must_be_audio_from_this_workspace(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    image = await _media(ws_id, kind="image")
    bad_kind = await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}", json={"intro": {"media_id": image, "name": "x"}}, headers=_h(ws_id),
    )
    assert bad_kind.status_code == 400

    other, _, other_ws, _ = await _setup(signup_user)
    foreign = await _media(other_ws)
    cross = await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}", json={"intro": {"media_id": foreign, "name": "x"}}, headers=_h(ws_id),
    )
    assert cross.status_code == 400
    assert (await client.get("/api/v1/audio-assets/kit/not-a-brand", headers=_h(ws_id))).status_code == 404


async def test_music_beds_can_be_added_listed_and_removed(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    media = await _media(ws_id)
    added = await client.post(
        f"/api/v1/audio-assets/kit/{brand_id}/beds", json={"name": "Lo-fi loop", "media_id": media}, headers=_h(ws_id),
    )
    assert added.status_code == 201, added.text
    bed = added.json()["music_beds"][0]
    assert bed["name"] == "Lo-fi loop" and bed["url"].endswith(".wav")

    assert (await client.post(
        f"/api/v1/audio-assets/kit/{brand_id}/beds", json={"name": "  ", "media_id": media}, headers=_h(ws_id),
    )).status_code == 400

    removed = await client.delete(f"/api/v1/audio-assets/kit/{brand_id}/beds/{bed['id']}", headers=_h(ws_id))
    assert removed.status_code == 200 and removed.json()["music_beds"] == []
    assert (await client.delete(f"/api/v1/audio-assets/kit/{brand_id}/beds/nope", headers=_h(ws_id))).status_code == 404


async def test_the_sponsor_script_can_be_voiced(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    no_script = await client.post(f"/api/v1/audio-assets/kit/{brand_id}/sponsor/synthesize", headers=_h(ws_id))
    assert no_script.status_code == 400

    await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}",
        json={"sponsor_name": "Zendly", "sponsor_script": "Brought to you by Zendly."}, headers=_h(ws_id),
    )
    res = await client.post(f"/api/v1/audio-assets/kit/{brand_id}/sponsor/synthesize", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["sponsor_clip"]["name"] == "Zendly (voiced)"
    assert stubs["synth"][-1]["text"] == "Brought to you by Zendly."


# ── assemble endpoint ────────────────────────────────────────────────────────

@pytest.fixture
def audio_files(monkeypatch):
    """Whatever the endpoint downloads is a real tone whose pitch says which
    piece it is, keyed by the media id it is asked for."""
    tones = {"voice": (220, 3.0), "intro": (600, 1.0), "outro": (900, 1.0), "sponsor": (700, 1.0), "bed": (1100, 2.0)}
    registry: dict[str, bytes] = {}

    async def _download(url):
        return registry[url]

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)

    def register(url: str, name: str):
        freq, seconds = tones[name]
        registry[url] = _wav(_tone(freq, seconds, 0.3))

    return register


async def _asset_with_kit(client, ws_id, brand_id, audio_files):
    asset = (await _generate(client, ws_id, brand_id, title="Ep")).json()
    voice_media = await media_assets.find_one({"id": asset["media_id"]})
    audio_files(voice_media["url"], "voice")

    ids = {}
    for name in ("intro", "outro", "sponsor", "bed"):
        ids[name] = await _media(ws_id)
        audio_files(f"https://res.example.com/{ids[name]}.wav", name)
    await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}",
        json={
            "intro": {"media_id": ids["intro"], "name": "Sting.wav"},
            "outro": {"media_id": ids["outro"], "name": "Out.wav"},
            "sponsor_clip": {"media_id": ids["sponsor"], "name": "Read.wav"},
            "sponsor_name": "Zendly",
        },
        headers=_h(ws_id),
    )
    bed = (await client.post(
        f"/api/v1/audio-assets/kit/{brand_id}/beds", json={"name": "Lo-fi", "media_id": ids["bed"]}, headers=_h(ws_id),
    )).json()["music_beds"][0]
    return asset, bed


async def test_assemble_builds_a_new_file_and_records_what_went_in(signup_user, stubs, audio_files):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, bed = await _asset_with_kit(client, ws_id, brand_id, audio_files)

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/assemble",
        json={"use_intro": True, "use_outro": True, "sponsor_at_s": 1.5, "music_bed_id": bed["id"]},
        headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    updated = res.json()

    assert updated["media_id"] == asset["media_id"]  # the recording itself is untouched
    assert updated["version_count"] == asset["version_count"]
    assembly = updated["assemblies"][-1]
    c = assembly["components"]
    assert c["intro_name"] == "Sting.wav" and c["outro_name"] == "Out.wav"
    assert c["sponsor_name"] == "Zendly" and c["music_bed_name"] == "Lo-fi"
    assert c["total_seconds"] == pytest.approx(3.0 + 1.0 + 1.0 + 1.0, abs=0.3)
    assert (await media_assets.find_one({"id": assembly["media_id"]}))["mime_type"] == "audio/wav"
    # The mixed file is what was stored.
    assert len(stubs["uploads"]) >= 2


async def test_assemble_never_replaces_an_approved_master(signup_user, stubs, audio_files):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, _ = await _asset_with_kit(client, ws_id, brand_id, audio_files)
    approved = (await client.patch(f"/api/v1/audio-assets/{asset['id']}/approve", headers=_h(ws_id))).json()

    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/assemble", json={"use_intro": True}, headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["approved_master_media_id"] == approved["approved_master_media_id"]


async def test_assemble_validation(signup_user, stubs, audio_files):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    voice = await media_assets.find_one({"id": asset["media_id"]})
    audio_files(voice["url"], "voice")
    url = f"/api/v1/audio-assets/{asset['id']}/assemble"

    assert (await client.post(url, json={}, headers=_h(ws_id))).status_code == 400          # nothing chosen
    no_intro = await client.post(url, json={"use_intro": True}, headers=_h(ws_id))          # kit is empty
    assert no_intro.status_code == 400 and "intro" in no_intro.json()["detail"]
    assert (await client.post(url, json={"music_bed_id": "nope"}, headers=_h(ws_id))).status_code == 400
    assert (await client.post(url, json={"music_level_db": 9}, headers=_h(ws_id))).status_code == 422
    assert (await client.post("/api/v1/audio-assets/nope/assemble", json={"use_intro": True}, headers=_h(ws_id))).status_code == 404

    doc = await audio_assets.find_one({"id": asset["id"]})
    assert doc.get("assemblies", []) == []  # every refused attempt left nothing behind
