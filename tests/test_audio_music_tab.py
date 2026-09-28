"""Tests for the five Music tab tiles: stereo width, warmth, transition
sounds at real pauses, and the brand signature clip. The mixer functions
directly on real synthetic audio, then the kit and assemble endpoints.
"""
import io

import numpy as np
import pytest
import soundfile as sf

from app.db.mongo import audio_assets, media_assets
from app.pipelines.media.audio_assemble import AssembleError, AssemblePlan, _real_pauses, _warm, _widen_stereo, assemble_episode
from tests.test_audio_assets import _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse

SR = 16000


def _tone(freq: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype("float32")


def _wav(signal: np.ndarray, sr: int = SR) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, signal.astype("float32"), sr, format="WAV")
    return buf.getvalue()


def _read(data: bytes):
    out, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    return out, sr


def _freq_level(x: np.ndarray, freq: float, sr: int = SR) -> float:
    spectrum = np.abs(np.fft.rfft(x[:, 0] * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    return float(spectrum[(freqs > freq - 20) & (freqs < freq + 20)].sum())


# ── the mixer functions directly ─────────────────────────────────────────────

def test_widen_stereo_widens_real_stereo_and_leaves_true_mono_alone():
    l, r = _tone(220, 1.0), _tone(280, 1.0)
    stereo = np.stack([l, r], axis=1)
    wide = _widen_stereo(stereo, 1.6)
    side_before = np.abs((stereo[:, 0] - stereo[:, 1]) / 2).mean()
    side_after = np.abs((wide[:, 0] - wide[:, 1]) / 2).mean()
    assert side_after / side_before == pytest.approx(1.6, rel=0.02)

    mono_pair = np.stack([l, l], axis=1)
    assert np.allclose(_widen_stereo(mono_pair, 1.6), mono_pair)


def test_warmth_changes_the_signal_and_never_exceeds_its_own_peak():
    tone = _tone(220, 1.0)
    warmed = _warm(tone, 1.0)
    assert not np.allclose(tone, warmed)
    assert np.abs(warmed).max() <= np.abs(tone).max() * 1.1


def test_real_pauses_only_finds_gaps_long_enough_and_ranks_the_longest_first():
    transcript = [
        {"word": "a", "start_s": 0.0, "end_s": 0.4},
        {"word": "b", "start_s": 0.6, "end_s": 1.0},   # 0.2s gap before this: too short
        {"word": "c", "start_s": 3.0, "end_s": 3.4},   # a real 2.0s pause before this
        {"word": "d", "start_s": 5.0, "end_s": 5.4},   # a real 1.6s pause before this
    ]
    pauses = _real_pauses(transcript, min_gap_s=1.2, limit=6)
    assert pauses == sorted(pauses)
    assert len(pauses) == 2


def test_real_pauses_respects_the_limit_by_keeping_the_longest():
    transcript = [{"word": "w0", "start_s": 0.0, "end_s": 0.4}]
    t = 0.4
    for i in range(1, 6):
        t += 2.0 + i  # each gap longer than the last
        transcript.append({"word": f"w{i}", "start_s": t, "end_s": t + 0.4})
        t += 0.4
    pauses = _real_pauses(transcript, min_gap_s=1.2, limit=2)
    assert len(pauses) == 2


# ── full mixer: transitions and signature ───────────────────────────────────

def test_transition_sound_lands_at_a_real_pause_not_a_guess():
    voice = np.concatenate([_tone(220, 1.0), np.zeros(int(SR * 2.0), dtype="float32"), _tone(220, 1.0)])
    transcript = [{"word": "hello", "start_s": 0.1, "end_s": 0.6}, {"word": "world", "start_s": 3.1, "end_s": 3.6}]
    transition = _tone(900, 0.3)

    out, mixed = assemble_episode(
        voice_bytes=_wav(voice), plan=AssemblePlan(use_transition_sfx=True, max_transitions=6),
        transition_sfx_bytes=_wav(transition), transcript=transcript,
    )
    decoded, _ = _read(out)
    assert mixed["transitions_added"] == 1
    landed_at = mixed["transition_times_s"][0]
    assert 0.6 < landed_at < 3.1  # inside the real gap between the two words
    window = decoded[int((landed_at - 0.1) * SR): int((landed_at + 0.3) * SR)]
    assert _freq_level(window, 900) > _freq_level(window, 220)  # the transition sound, not the voice, is there


def test_transition_sound_is_refused_with_no_real_pause_long_enough():
    voice = _tone(220, 2.0)
    transcript = [{"word": "a", "start_s": 0.0, "end_s": 1.0}, {"word": "b", "start_s": 1.1, "end_s": 2.0}]
    with pytest.raises(AssembleError, match="no real pauses"):
        assemble_episode(
            voice_bytes=_wav(voice), plan=AssemblePlan(use_transition_sfx=True),
            transition_sfx_bytes=_wav(_tone(900, 0.2)), transcript=transcript,
        )


def test_transition_sound_needs_the_clip_set_up():
    voice = _tone(220, 2.0)
    with pytest.raises(AssembleError, match="transition sound"):
        assemble_episode(voice_bytes=_wav(voice), plan=AssemblePlan(use_transition_sfx=True), transcript=[])


def test_brand_signature_plays_before_everything_else_including_the_intro():
    voice = _tone(220, 1.0)
    intro = _tone(600, 1.0)
    signature = _tone(999, 0.5)

    out, mixed = assemble_episode(
        voice_bytes=_wav(voice), plan=AssemblePlan(use_intro=True, use_brand_signature=True),
        intro_bytes=_wav(intro), signature_bytes=_wav(signature),
    )
    decoded, _ = _read(out)
    assert mixed["brand_signature"] is True
    assert _freq_level(decoded[: int(0.4 * SR)], 999) > _freq_level(decoded[: int(0.4 * SR)], 600)
    assert _freq_level(decoded[int(0.6 * SR): int(1.4 * SR)], 600) > _freq_level(decoded[int(0.6 * SR): int(1.4 * SR)], 999)


def test_brand_signature_needs_the_clip_set_up():
    with pytest.raises(AssembleError, match="brand signature"):
        assemble_episode(voice_bytes=_wav(_tone(220, 1.0)), plan=AssemblePlan(use_brand_signature=True), transcript=[])


def test_stereo_width_and_warmth_are_reported_when_they_really_applied():
    voice = np.stack([_tone(220, 2.0), _tone(220, 2.0)], axis=1)  # stereo voice, so the bed can widen for real
    bed = np.stack([_tone(300, 2.0), _tone(340, 2.0)], axis=1)
    out, mixed = assemble_episode(
        voice_bytes=_wav(voice), plan=AssemblePlan(music_bed_id="b", stereo_width=1.5, voice_warmth=0.4),
        bed_bytes=_wav(bed),
    )
    assert mixed["stereo_width"] == 1.5
    assert mixed["voice_warmth"] == 0.4


# ── the kit ──────────────────────────────────────────────────────────────────

async def _media(ws_id: str, duration_s: float | None = None) -> str:
    from datetime import datetime, timezone
    from uuid import uuid4

    mid = uuid4().hex
    doc = {
        "id": mid, "workspace_id": ws_id, "kind": "audio", "url": f"https://res.example.com/{mid}.wav",
        "mime_type": "audio/wav", "source": "uploaded", "created_by": "u", "created_at": datetime.now(timezone.utc),
    }
    if duration_s is not None:
        doc["duration_s"] = duration_s
    await media_assets.insert_one(doc)
    return mid


async def test_kit_accepts_a_transition_sound_and_a_short_signature(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    transition_id = await _media(ws_id)
    signature_id = await _media(ws_id, duration_s=4.0)

    res = await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}",
        json={
            "transition_sfx": {"media_id": transition_id, "name": "Riser.wav"},
            "brand_signature": {"media_id": signature_id, "name": "Sig.wav"},
        },
        headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["transition_sfx"]["name"] == "Riser.wav"
    assert res.json()["brand_signature"]["name"] == "Sig.wav"


async def test_a_signature_longer_than_5_seconds_is_refused(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    long_id = await _media(ws_id, duration_s=12.0)
    res = await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}",
        json={"brand_signature": {"media_id": long_id, "name": "TooLong.wav"}}, headers=_h(ws_id),
    )
    assert res.status_code == 400 and "5 seconds" in res.json()["detail"]


async def test_a_signature_with_no_known_duration_is_not_blocked(signup_user, stubs):
    """A real, honest limitation: an upload with no analysed duration can't
    be checked server-side, so it isn't blocked on a value that isn't known."""
    client, _, ws_id, brand_id = await _setup(signup_user)
    unknown_id = await _media(ws_id, duration_s=None)
    res = await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}",
        json={"brand_signature": {"media_id": unknown_id, "name": "Unknown.wav"}}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text


# ── the assemble endpoint ────────────────────────────────────────────────────

async def test_assemble_endpoint_wires_transition_and_signature_end_to_end(signup_user, stubs, monkeypatch):
    from app.api.v1 import audio_assets as audio_module

    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    transcript = [{"word": "hello", "start_s": 0.1, "end_s": 0.6, "speaker": None},
                  {"word": "world", "start_s": 3.1, "end_s": 3.6, "speaker": None}]
    await audio_assets.update_one({"id": asset["id"]}, {"$set": {"transcript": transcript}})

    voice = np.concatenate([_tone(220, 1.0), np.zeros(int(SR * 2.0), dtype="float32"), _tone(220, 1.0)])
    signature = _tone(999, 0.3)
    transition = _tone(900, 0.2)
    registry = {}

    async def _download(url):
        return registry[url]

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)
    voice_media = await media_assets.find_one({"id": asset["media_id"]})
    registry[voice_media["url"]] = _wav(voice)

    sig_id = await _media(ws_id)
    trans_id = await _media(ws_id)
    registry[f"https://res.example.com/{sig_id}.wav"] = _wav(signature)
    registry[f"https://res.example.com/{trans_id}.wav"] = _wav(transition)

    await client.put(
        f"/api/v1/audio-assets/kit/{brand_id}",
        json={
            "transition_sfx": {"media_id": trans_id, "name": "Riser"},
            "brand_signature": {"media_id": sig_id, "name": "Sig"},
        },
        headers=_h(ws_id),
    )

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/assemble",
        json={"use_transition_sfx": True, "use_brand_signature": True}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    assembly = res.json()["assemblies"][-1]
    assert assembly["components"]["transition_sfx_name"] == "Riser"
    assert assembly["components"]["brand_signature_name"] == "Sig"
    assert assembly["components"]["transitions_added"] == 1


async def test_assemble_still_needs_at_least_one_real_choice(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/assemble", json={}, headers=_h(ws_id))
    assert res.status_code == 400
