"""Tests for the two P2-4 cleanup tiles: mouth-click removal and room-tone
fill. Real signal processing on real synthetic audio, same style as
test_audio_cleanup.py — every assertion measures the actual result.
"""
import io

import numpy as np
import pytest
import soundfile as sf

from app.pipelines.media.audio_cleanup import CleanupError, CleanupSettings, _declick, _extract_room_tone, apply_cleanup
from tests.test_audio_assets import stubs  # noqa: F401 — fixture reuse

SR = 16000


def _tone(freq: float, seconds: float, amp: float = 0.3, phase: float = 0.7) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * freq * t + phase)).astype("float32")


def _encode(signal: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, signal.astype("float32"), SR, format="WAV")
    return buf.getvalue()


def _decode(data: bytes) -> np.ndarray:
    out, sr = sf.read(io.BytesIO(data), dtype="float32")
    assert sr == SR
    return out


# ── mouth-click removal ──────────────────────────────────────────────────────

def test_declick_repairs_a_real_spike_and_leaves_the_rest_untouched():
    tone = _tone(220, 1.0)
    clicky = tone.copy()
    clicky[5000:5003] = 0.95  # a real short, sharp spike — a click
    out = _declick(clicky, SR, 1.0)

    assert np.abs(out[5000:5003]).max() < 0.4  # the spike is gone
    outside = np.r_[0:4990, 5013:len(out)]
    assert np.allclose(out[outside], tone[outside], atol=1e-6)  # nothing else moved


def test_declick_leaves_real_loud_speech_alone():
    """A run longer than a real click (here, a whole loud word) must not
    be flattened — that would be audible damage, not a repair."""
    tone = _tone(220, 1.0, amp=0.3)
    loud = tone.copy()
    loud[4000:4400] = _tone(220, 0.025, amp=0.9)  # 25ms of genuinely loud speech
    out = _declick(loud, SR, 1.0)
    assert np.abs(out[4000:4400]).max() > 0.5  # still there, not interpolated flat


def test_declick_strength_changes_how_small_a_spike_it_catches():
    tone = _tone(220, 1.0)
    mild_click = tone.copy()
    mild_click[6000:6002] = 0.85  # a borderline spike: enough for the strong setting to catch, not the weak one

    weak = _declick(mild_click, SR, 0.05)
    strong = _declick(mild_click, SR, 1.0)
    assert np.abs(strong[6000:6002] - mild_click[6000:6002]).max() > np.abs(weak[6000:6002] - mild_click[6000:6002]).max()


async def test_declick_via_the_real_cleanup_endpoint(signup_user, stubs, monkeypatch):
    from app.api.v1 import audio_assets as audio_module
    from tests.test_audio_assets import _generate, _h, _setup

    tone = _tone(220, 2.0)
    clicky = tone.copy()
    clicky[16000:16003] = 0.95

    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    async def _download(url):
        return _encode(clicky)

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)
    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/cleanup", json={"mouth_click_removal": 1.0}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["dsp_settings"]["cleanup"]["mouth_click_removal"] == 1.0


# ── room-tone fill ───────────────────────────────────────────────────────────

def test_room_tone_is_a_real_slice_of_the_quiet_part_of_this_recording():
    loud = _tone(220, 1.0, amp=0.3)
    quiet = 0.001 * np.random.default_rng(1).standard_normal(SR).astype("float32")
    signal = np.concatenate([loud, quiet, loud])

    tone = _extract_room_tone(signal, SR, seconds=0.5)
    assert len(tone) == pytest.approx(0.5 * SR, abs=1)
    assert np.sqrt(np.mean(tone ** 2)) < 0.01  # it's real, quiet ambience, not the loud tone


def test_room_tone_fill_needs_a_real_cut_to_apply_to():
    with pytest.raises(CleanupError, match="Silence Trimmer"):
        apply_cleanup(_encode(_tone(220, 1.0)), CleanupSettings(room_tone_fill=True), [])


def test_room_tone_fill_adds_real_ambience_across_a_cut_seam():
    loud = _tone(220, 1.0, amp=0.3)
    gap = 0.0008 * np.random.default_rng(2).standard_normal(int(SR * 2.0)).astype("float32")
    signal = np.concatenate([loud, gap, loud])

    without = apply_cleanup(_encode(signal), CleanupSettings(silence_trim_s=0.5), [])[0]
    withit = apply_cleanup(_encode(signal), CleanupSettings(silence_trim_s=0.5, room_tone_fill=True), [])[0]
    seam_without = np.abs(_decode(without)).mean()
    seam_with = np.abs(_decode(withit)).mean()
    # The filled version genuinely carries more signal through the join than
    # a bare crossfade of two near-silent edges does.
    assert seam_with > seam_without


def test_room_tone_fill_does_not_change_the_kept_speech():
    loud = _tone(220, 1.0, amp=0.3)
    gap = np.zeros(int(SR * 2.0), dtype="float32")
    signal = np.concatenate([loud, gap, loud])

    out, _, applied = apply_cleanup(_encode(signal), CleanupSettings(silence_trim_s=0.5, room_tone_fill=True), [])
    assert applied["room_tone_fill"] is True
    decoded = _decode(out)
    # The speech itself (well away from the seam) still sounds like the speech.
    assert np.abs(decoded[: int(0.5 * SR)]).mean() == pytest.approx(np.abs(loud[: int(0.5 * SR)]).mean(), rel=0.05)


def test_room_tone_fill_never_clips_the_result():
    loud = _tone(220, 1.0, amp=0.9)
    gap = np.zeros(int(SR * 2.0), dtype="float32")
    signal = np.concatenate([loud, gap, loud])
    out = apply_cleanup(_encode(signal), CleanupSettings(silence_trim_s=0.5, room_tone_fill=True), [])[0]
    assert np.abs(_decode(out)).max() <= 0.995
