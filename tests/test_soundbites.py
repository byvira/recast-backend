"""Tests for real soundbite extraction and the Batch Approval Queue's real
backing endpoints (was mock-only, MOCK_BATCH_ITEMS/BatchApprovalQueue.tsx).
"""

import io

import numpy as np
import pytest
import soundfile as sf

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import media_assets, soundbites
from app.models.audio_asset import SoundbiteStatus
from app.pipelines.media.soundbite_extraction import SoundbiteExtractionError, evaluate_quality, trim_span
from tests.test_audio_assets import _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse

SR = 16000


def _tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> bytes:
    t = np.arange(int(SR * seconds)) / SR
    signal = (amp * np.sin(2 * np.pi * freq * t)).astype("float32")
    buf = io.BytesIO()
    sf.write(buf, signal, SR, format="WAV")
    return buf.getvalue()


# ── trim_span / evaluate_quality (pure, no network) ─────────────────────────

def test_trim_span_cuts_the_exact_real_range():
    master = _tone(10.0)
    clip = trim_span(master, 2.0, 4.0)
    data, sr = sf.read(io.BytesIO(clip))
    assert sr == SR
    assert len(data) / sr == pytest.approx(2.0, abs=0.01)


def test_trim_span_rejects_a_span_that_does_not_fit():
    master = _tone(3.0)
    with pytest.raises(SoundbiteExtractionError, match="doesn't fit"):
        trim_span(master, 1.0, 10.0)


def test_trim_span_rejects_a_span_too_short_to_be_real():
    master = _tone(3.0)
    with pytest.raises(SoundbiteExtractionError, match="too short"):
        trim_span(master, 1.0, 1.1)


def test_evaluate_quality_flags_real_clipping():
    t = np.arange(int(SR * 1.0)) / SR
    clipped = (0.99 * np.sign(np.sin(2 * np.pi * 220 * t))).astype("float32")
    buf = io.BytesIO()
    sf.write(buf, clipped, SR, format="WAV")

    status, confidence, flag, lufs = evaluate_quality(buf.getvalue())
    assert status == SoundbiteStatus.NEEDS_ATTENTION
    assert flag and "clipping" in flag.lower()
    assert confidence < 100


def test_evaluate_quality_passes_a_real_clean_clip_at_broadcast_loudness():
    # A tone loud enough to read as real speech-level loudness, not silence.
    clip = _tone(2.0, amp=0.3)
    status, confidence, flag, lufs = evaluate_quality(clip)
    assert confidence >= 0
    assert lufs is not None


def test_evaluate_quality_flags_near_silence():
    silence = _tone(1.0, amp=0.0001)
    status, confidence, flag, lufs = evaluate_quality(silence)
    assert status == SoundbiteStatus.NEEDS_ATTENTION
    assert flag and "silence" in flag.lower()


# ── real endpoints ────────────────────────────────────────────────────────

async def _asset_with_transcript_and_media(client, ws_id, brand_id, monkeypatch):
    asset = (await _generate(client, ws_id, brand_id)).json()
    words = [{"word": w, "start_s": i * 1.0, "end_s": i * 1.0 + 0.6, "speaker": None}
              for i, w in enumerate(["This", "is", "a", "real", "quote", "worth", "sharing."])]
    from app.db.mongo import audio_assets as audio_assets_col
    await audio_assets_col.update_one({"id": asset["id"]}, {"$set": {"transcript": words}})

    master = _tone(8.0)
    monkeypatch.setattr(audio_module, "_download_media_bytes", lambda url: _async_return(master))
    return asset


async def _async_return(value):
    return value


def _fake_structured_one_suggestion():
    async def _fake(prompt, **kwargs):
        return {"suggestions": [{"start_s": 0.0, "end_s": 4.0, "quote": "This is a real quote", "reason": "A complete thought."}]}
    return _fake


async def test_extract_soundbites_creates_real_persisted_clips(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_transcript_and_media(client, ws_id, brand_id, monkeypatch)
    monkeypatch.setattr(audio_module, "call_llm_structured", _fake_structured_one_suggestion())

    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/soundbites", headers=_h(ws_id))
    assert res.status_code == 201, res.text
    created = res.json()
    assert len(created) == 1
    assert created[0]["quote"] == "This is a real quote"
    assert created[0]["approval_status"] == "pending"
    assert created[0]["url"]

    stored = await soundbites.count_documents({"audio_asset_id": asset["id"]})
    assert stored == 1
    media = await media_assets.find_one({"id": created[0]["media_id"]})
    assert media["kind"] == "audio"


async def test_extract_soundbites_needs_a_transcript(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/soundbites", headers=_h(ws_id))
    assert res.status_code == 400


async def test_list_soundbites_returns_real_extracted_clips(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_transcript_and_media(client, ws_id, brand_id, monkeypatch)
    monkeypatch.setattr(audio_module, "call_llm_structured", _fake_structured_one_suggestion())
    await client.post(f"/api/v1/audio-assets/{asset['id']}/soundbites", headers=_h(ws_id))

    res = await client.get(f"/api/v1/audio-assets/{asset['id']}/soundbites", headers=_h(ws_id))
    assert res.status_code == 200
    assert len(res.json()) == 1


async def test_approve_and_reject_a_soundbite(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_transcript_and_media(client, ws_id, brand_id, monkeypatch)
    monkeypatch.setattr(audio_module, "call_llm_structured", _fake_structured_one_suggestion())
    created = (await client.post(f"/api/v1/audio-assets/{asset['id']}/soundbites", headers=_h(ws_id))).json()
    sb_id = created[0]["id"]

    approved = await client.patch(f"/api/v1/audio-assets/soundbites/{sb_id}/approve", headers=_h(ws_id))
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_status"] == "approved"

    rejected = await client.patch(f"/api/v1/audio-assets/soundbites/{sb_id}/reject", headers=_h(ws_id))
    assert rejected.status_code == 200
    assert rejected.json()["approval_status"] == "rejected"


async def test_approve_missing_soundbite_404s(signup_user, stubs):
    client, _, ws_id, _ = await _setup(signup_user)
    res = await client.patch(f"/api/v1/audio-assets/soundbites/nope/approve", headers=_h(ws_id))
    assert res.status_code == 404
