"""Tests for the member-controlled cleanup dials. Real signal processing on
real synthetic audio: every assertion measures the result (levels, spectrum,
length, word timings), it doesn't just check that a function was called.
"""
import io

import numpy as np
import pyloudnorm as pyln
import pytest
import soundfile as sf

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import audio_asset_versions, audio_assets, media_assets
from app.pipelines.media import audio_cleanup
from app.pipelines.media.audio_cleanup import CleanupError, CleanupSettings, apply_cleanup
from tests.test_audio_assets import _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse

SR = 16000


def _encode(signal: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, signal.astype("float32"), SR, format="WAV")
    return buf.getvalue()


def _decode(data: bytes) -> np.ndarray:
    out, sr = sf.read(io.BytesIO(data), dtype="float32")
    assert sr == SR
    return out


def _tone(freq: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


def _band_level(signal: np.ndarray, freq: float, width: float = 15.0) -> float:
    # Windowed, so a loud tone elsewhere doesn't leak into the band measured.
    spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
    freqs = np.fft.rfftfreq(len(signal), 1 / SR)
    return float(spectrum[(freqs > freq - width) & (freqs < freq + width)].sum())


def _speech_with_gap() -> tuple[np.ndarray, list[dict]]:
    """1s speech-like tone, 2s of silence, 1s tone. Words sit inside the tones."""
    signal = np.concatenate([_tone(220, 1.0), np.zeros(int(SR * 2.0)), _tone(220, 1.0)])
    transcript = [
        {"word": "hello", "start_s": 0.1, "end_s": 0.6, "speaker": None},
        {"word": "again", "start_s": 3.1, "end_s": 3.6, "speaker": None},
    ]
    return signal, transcript


def _run(signal, settings, transcript=None):
    return apply_cleanup(_encode(signal), CleanupSettings(**settings), transcript or [])


# ── settings ─────────────────────────────────────────────────────────────────

def test_nothing_selected_is_refused():
    with pytest.raises(CleanupError, match="at least one"):
        _run(_tone(220, 1), {})


def test_out_of_range_values_are_rejected_by_the_model():
    from pydantic import ValidationError

    for bad in ({"noise_reduction": 5}, {"target_lufs": 0}, {"highpass_hz": 10}, {"crossfade_ms": 500}, {"silence_trim_s": 0.01}):
        with pytest.raises(ValidationError):
            CleanupSettings(**bad)


def test_an_undecodable_or_too_long_file_is_refused(monkeypatch):
    with pytest.raises(CleanupError, match="format"):
        apply_cleanup(b"not audio at all", CleanupSettings(compressor=0.5), [])
    monkeypatch.setattr(audio_cleanup, "MAX_SECONDS", 1)
    with pytest.raises(CleanupError, match="30 minutes"):
        _run(_tone(220, 2.0), {"compressor": 0.5})


# ── time cuts ────────────────────────────────────────────────────────────────

def test_silence_trim_shortens_long_gaps_and_moves_the_words():
    signal, transcript = _speech_with_gap()
    wav, new_transcript, applied = _run(signal, {"silence_trim_s": 0.5}, transcript)
    out = _decode(wav)

    # The 2s gap keeps 0.5s: about 1.5s shorter.
    assert len(signal) / SR - len(out) / SR == pytest.approx(1.5, abs=0.1)
    assert applied["gaps_shortened"] == 1 and applied["seconds_removed"] == pytest.approx(1.5, abs=0.1)
    assert new_transcript[0]["start_s"] == pytest.approx(0.1, abs=0.01)     # before the cut: unchanged
    assert new_transcript[1]["start_s"] == pytest.approx(3.1 - 1.5, abs=0.1)  # after it: earlier by the cut
    # And the moved word really is where the sound is.
    start = int(new_transcript[1]["start_s"] * SR)
    assert np.abs(out[start + 200: start + 3000]).mean() > 0.05


def test_gaps_shorter_than_the_setting_are_left_alone():
    signal, transcript = _speech_with_gap()
    wav, new_transcript, applied = _run(signal, {"silence_trim_s": 2.0}, transcript)
    assert applied["gaps_shortened"] == 0
    assert len(_decode(wav)) == pytest.approx(len(signal), abs=10)
    assert new_transcript == transcript


def test_filler_words_are_cut_from_audio_and_transcript():
    signal = np.concatenate([_tone(220, 1.0), _tone(600, 0.5), _tone(220, 1.0)])
    transcript = [
        {"word": "hello", "start_s": 0.1, "end_s": 0.9, "speaker": None},
        {"word": "Um,", "start_s": 1.0, "end_s": 1.5, "speaker": None},
        {"word": "world", "start_s": 1.6, "end_s": 2.4, "speaker": None},
    ]
    wav, new_transcript, applied = _run(signal, {"remove_fillers": True}, transcript)
    out = _decode(wav)

    assert applied["fillers_removed"] == 1
    assert [w["word"] for w in new_transcript] == ["hello", "world"]
    assert len(signal) / SR - len(out) / SR == pytest.approx(0.5, abs=0.05)
    assert new_transcript[1]["start_s"] == pytest.approx(1.1, abs=0.05)
    # The 600 Hz "um" is gone, the 220 Hz speech is still there.
    assert _band_level(out, 600) < 0.1 * _band_level(signal, 600)
    assert _band_level(out, 220) > 0.5 * _band_level(signal, 220)


def test_filler_removal_needs_a_transcript():
    with pytest.raises(CleanupError, match="needs a transcript"):
        _run(_tone(220, 1.0), {"remove_fillers": True}, [])


# ── filters and dynamics ─────────────────────────────────────────────────────

def test_rumble_and_hiss_filters_remove_only_what_they_target():
    signal = _tone(30, 2.0, 0.3) + _tone(220, 2.0, 0.3) + _tone(7000, 2.0, 0.3)
    hp = _decode(_run(signal, {"highpass_hz": 100})[0])
    assert _band_level(hp, 30) < 0.1 * _band_level(signal, 30)
    assert _band_level(hp, 220) > 0.8 * _band_level(signal, 220)

    lp = _decode(_run(signal, {"lowpass_hz": 4000})[0])
    assert _band_level(lp, 7000, 100) < 0.1 * _band_level(signal, 7000, 100)
    assert _band_level(lp, 220) > 0.8 * _band_level(signal, 220)


def test_loudness_lands_on_the_target():
    signal = _tone(220, 5.0, 0.05)
    wav, _, applied = _run(signal, {"target_lufs": -16.0})
    measured = pyln.Meter(SR).integrated_loudness(_decode(wav))
    assert measured == pytest.approx(-16.0, abs=1.0)
    assert applied["target_lufs"] == -16.0 and "measured_loudness_before_lufs" in applied
    assert np.abs(_decode(wav)).max() <= 0.99  # never clipped


def test_compressor_narrows_the_gap_between_loud_and_quiet():
    signal = np.concatenate([_tone(220, 2.0, 0.6), _tone(220, 2.0, 0.05)])
    out = _decode(_run(signal, {"compressor": 1.0})[0])

    def gap(x):
        loud, quiet = np.abs(x[: 2 * SR]).mean(), np.abs(x[2 * SR:]).mean()
        return loud / quiet

    assert gap(out) < 0.6 * gap(signal)


def test_de_esser_turns_down_a_loud_harsh_band_only():
    voice = _tone(220, 3.0, 0.3)
    hiss = np.zeros(3 * SR)
    hiss[SR: 2 * SR] = _tone(7000, 1.0, 0.4)  # a harsh "s" in the middle second
    signal = voice + hiss
    out = _decode(_run(signal, {"de_esser": 1.0})[0])

    before = np.abs(np.fft.rfft(signal[SR: 2 * SR]))[7000]
    after = np.abs(np.fft.rfft(out[SR: 2 * SR]))[7000]
    assert after < 0.6 * before
    assert _band_level(out, 220) > 0.9 * _band_level(signal, 220)


def test_noise_reduction_lowers_the_noise_floor():
    rng = np.random.default_rng(3)
    noise = 0.05 * rng.standard_normal(4 * SR)
    signal = noise.copy()
    signal[SR: 2 * SR] += _tone(300, 1.0, 0.4)
    out = _decode(_run(signal, {"noise_reduction": 1.0})[0])
    assert np.abs(out[3 * SR:]).mean() < 0.5 * np.abs(signal[3 * SR:]).mean()


def test_stereo_stays_stereo():
    stereo = np.stack([_tone(220, 1.0), _tone(330, 1.0)], axis=1)
    out, sr = sf.read(io.BytesIO(apply_cleanup(_encode(stereo), CleanupSettings(highpass_hz=100), [])[0]))
    assert out.shape[1] == 2 and sr == SR


# ── endpoint ─────────────────────────────────────────────────────────────────

@pytest.fixture
def cleanup_source(monkeypatch):
    """Serve a real speech-with-a-gap file whenever the endpoint downloads the
    current recording."""
    signal, _ = _speech_with_gap()

    async def _download(url):
        return _encode(signal)

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)
    return signal


async def _uploaded_with_transcript(client, ws_id, brand_id):
    """A recording with a transcript whose words match _speech_with_gap()."""
    asset = (await _generate(client, ws_id, brand_id)).json()
    transcript = _speech_with_gap()[1]
    await audio_assets.update_one({"id": asset["id"]}, {"$set": {"transcript": transcript}})
    # As if the transcript had been there from the start (it is, for an upload).
    await audio_asset_versions.update_one(
        {"audio_asset_id": asset["id"], "version_number": 1}, {"$set": {"transcript_snapshot": transcript}},
    )
    return asset


async def test_cleanup_makes_a_new_restorable_version(signup_user, stubs, cleanup_source):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _uploaded_with_transcript(client, ws_id, brand_id)

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/cleanup",
        json={"silence_trim_s": 0.5, "target_lufs": -16}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    updated = res.json()

    assert updated["media_id"] != asset["media_id"]
    assert updated["version_count"] == 2
    assert updated["dsp_settings"]["cleanup"]["gaps_shortened"] == 1
    assert updated["transcript"][1]["start_s"] < 2.0            # moved earlier by the cut
    assert (await media_assets.find_one({"id": updated["media_id"]}))["source"] == "enhanced"

    versions = (await client.get(f"/api/v1/audio-assets/{asset['id']}/versions", headers=_h(ws_id))).json()["versions"]
    assert [v["action"] for v in versions] == ["created", "cleanup"]

    # Restoring the first version brings its audio AND its word timings back.
    restored = (await client.post(f"/api/v1/audio-assets/{asset['id']}/restore/1", headers=_h(ws_id))).json()
    assert restored["media_id"] == asset["media_id"]
    assert restored["transcript"][1]["start_s"] == pytest.approx(3.1)


async def test_cleanup_never_replaces_an_approved_master(signup_user, stubs, cleanup_source):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _uploaded_with_transcript(client, ws_id, brand_id)
    approved = (await client.patch(f"/api/v1/audio-assets/{asset['id']}/approve", headers=_h(ws_id))).json()

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/cleanup", json={"compressor": 0.5}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["approved_master_media_id"] == approved["approved_master_media_id"]
    assert res.json()["media_id"] != approved["media_id"]


async def test_cleanup_endpoint_validation(signup_user, stubs, cleanup_source):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    url = f"/api/v1/audio-assets/{asset['id']}/cleanup"

    assert (await client.post(url, json={}, headers=_h(ws_id))).status_code == 400
    assert (await client.post(url, json={"noise_reduction": 9}, headers=_h(ws_id))).status_code == 422
    # A generated recording has no transcript, so filler removal has nothing to work from.
    filler = await client.post(url, json={"remove_fillers": True}, headers=_h(ws_id))
    assert filler.status_code == 400 and "transcript" in filler.json()["detail"]
    assert (await client.post("/api/v1/audio-assets/nope/cleanup", json={"compressor": 0.5}, headers=_h(ws_id))).status_code == 404

    versions = await audio_asset_versions.count_documents({"audio_asset_id": asset["id"]})
    assert versions == 1  # every refused attempt left no trace
