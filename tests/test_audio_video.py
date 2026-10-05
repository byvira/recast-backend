"""Tests for turning a recording into a video: the renderer itself on real
synthetic audio, and the two endpoints (video, suggest-clips).
"""
import io
import json

import numpy as np
import pytest
import soundfile as sf

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import audio_assets, brand_profiles, media_assets, podcast_feed_settings
from app.pipelines.media.video_render import (
    TranscriptWordLike,
    VideoRenderError,
    _hex_to_ass_bgr,
    _hex_to_ffmpeg_color,
    build_ass_captions,
    render_video,
)
from tests.test_audio_assets import _generate, _h, _setup, _wav, stubs  # noqa: F401 — fixture reuse

SR = 16000


def _tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> bytes:
    t = np.arange(int(SR * seconds)) / SR
    signal = (amp * np.sin(2 * np.pi * freq * t)).astype("float32")
    buf = io.BytesIO()
    sf.write(buf, signal, SR, format="WAV")
    return buf.getvalue()


def _words(n: int, step: float = 0.4) -> list[TranscriptWordLike]:
    return [TranscriptWordLike(word=f"word{i}", start_s=i * step, end_s=i * step + step * 0.7) for i in range(n)]


# ── color and caption helpers ────────────────────────────────────────────────

def test_hex_colors_convert_correctly_and_fall_back_on_garbage():
    assert _hex_to_ass_bgr("#38bdf8", "#000000") == "F8BD38"
    assert _hex_to_ass_bgr("not-a-color", "#38bdf8") == "F8BD38"
    assert _hex_to_ffmpeg_color("#38BDF8", "#000000") == "38BDF8"
    assert _hex_to_ffmpeg_color("", "#0f172a") == "0F172A"


def test_captions_only_cover_the_requested_window_and_escape_braces():
    words = [TranscriptWordLike(word=w, start_s=i * 1.0, end_s=i * 1.0 + 0.5) for i, w in enumerate(["a", "b{c}", "d"])]
    ass = build_ass_captions(words, start_s=0.5, end_s=1.6, video_w=1080, video_h=1080, accent_hex="#38bdf8")
    assert "{c}" not in ass and "(c)" in ass  # ASS braces are control syntax, real words get escaped
    assert "PlayResX: 1080" in ass and "F8BD38" in ass  # accent reaches the style line, not the fallback


def test_no_words_in_the_window_still_renders_a_valid_empty_track():
    ass = build_ass_captions([], start_s=0.0, end_s=2.0, video_w=1080, video_h=1080, accent_hex="#38bdf8")
    assert "[Events]" in ass


# ── the renderer, real end-to-end ────────────────────────────────────────────

async def test_solid_and_waveform_styles_produce_a_real_mp4_of_the_right_length():
    audio = _tone(3.0)
    for style in ("solid", "waveform"):
        out = await render_video(
            audio_bytes=audio, words=_words(5), start_s=0.0, end_s=3.0, style=style, size="square",
            background_hex="#0f172a", accent_hex="#38bdf8",
        )
        assert out[4:8] == b"ftyp", f"{style} didn't produce a real mp4"
        assert len(out) > 1000


async def test_cover_style_falls_back_honestly_when_there_is_no_cover():
    out = await render_video(
        audio_bytes=_tone(1.5), words=_words(3), start_s=0.0, end_s=1.5, style="cover", size="vertical",
        background_hex="#0f172a", accent_hex="#38bdf8", cover_bytes=None,
    )
    assert out[4:8] == b"ftyp"


async def test_a_zero_or_negative_duration_is_refused():
    with pytest.raises(VideoRenderError, match="after the start"):
        await render_video(
            audio_bytes=_tone(1.0), words=[], start_s=1.0, end_s=1.0, style="solid", size="square",
            background_hex="#0f172a", accent_hex="#38bdf8",
        )


async def test_longer_than_the_cap_is_refused(monkeypatch):
    from app.pipelines.media import video_render
    monkeypatch.setattr(video_render, "MAX_SECONDS", 1)
    with pytest.raises(VideoRenderError, match="limit"):
        await render_video(
            audio_bytes=_tone(2.0), words=[], start_s=0.0, end_s=2.0, style="solid", size="square",
            background_hex="#0f172a", accent_hex="#38bdf8",
        )


# ── the /video endpoint ──────────────────────────────────────────────────────

async def _with_transcript(client, ws_id, brand_id, seconds=3.0, n_words=6):
    asset = (await _generate(client, ws_id, brand_id)).json()
    words = [{"word": f"w{i}", "start_s": i * (seconds / n_words), "end_s": i * (seconds / n_words) + 0.3, "speaker": None} for i in range(n_words)]
    await audio_assets.update_one({"id": asset["id"]}, {"$set": {"transcript": words}})
    real = _tone(seconds)

    async def _download(url):
        return real

    return asset, _download


async def test_make_video_stores_a_real_clip_with_real_duration(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, download = await _with_transcript(client, ws_id, brand_id, seconds=4.0)
    monkeypatch.setattr(audio_module, "_download_media_bytes", download)
    media = await media_assets.find_one({"id": asset["media_id"]})
    await media_assets.update_one({"id": media["id"]}, {"$set": {"duration_s": 4.0}})

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/video",
        json={"start_s": 0.0, "end_s": 2.0, "style": "solid", "size": "square"}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    updated = res.json()
    clip = updated["video_clips"][-1]
    assert clip["style"] == "solid" and clip["size"] == "square"
    video_media = await media_assets.find_one({"id": clip["media_id"]})
    assert video_media["kind"] == "video" and video_media["mime_type"] == "video/mp4"
    assert video_media["duration_s"] == pytest.approx(2.0, abs=0.05)
    assert video_media["size_bytes"] and video_media["size_bytes"] > 0
    assert updated["media_id"] == asset["media_id"]  # the real recording itself is untouched


async def test_no_end_s_uses_the_recordings_own_real_duration(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, download = await _with_transcript(client, ws_id, brand_id, seconds=2.5)
    monkeypatch.setattr(audio_module, "_download_media_bytes", download)
    await media_assets.update_one({"id": asset["media_id"]}, {"$set": {"duration_s": 2.5}})

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/video",
        json={"style": "waveform", "size": "landscape"}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    video_media = await media_assets.find_one({"id": res.json()["video_clips"][-1]["media_id"]})
    assert video_media["duration_s"] == pytest.approx(2.5, abs=0.05)


async def test_cover_style_uses_the_feeds_real_cover_image(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, download_audio = await _with_transcript(client, ws_id, brand_id, seconds=2.0)
    await media_assets.update_one({"id": asset["media_id"]}, {"$set": {"duration_s": 2.0}})

    cover_id = "cover-1"
    await media_assets.insert_one({
        "id": cover_id, "workspace_id": ws_id, "kind": "image", "url": "https://res.example.com/cover.png",
        "mime_type": "image/png", "source": "uploaded", "created_by": "u",
        "created_at": (await audio_assets.find_one({"id": asset["id"]}))["created_at"],
    })
    await podcast_feed_settings.update_one(
        {"brand_id": brand_id, "workspace_id": ws_id},
        {"$set": {"id": brand_id, "workspace_id": ws_id, "brand_id": brand_id, "token": "t", "title": "Show",
                   "is_enabled": True, "cover_media_id": cover_id,
                   "created_at": (await audio_assets.find_one({"id": asset["id"]}))["created_at"],
                   "updated_at": (await audio_assets.find_one({"id": asset["id"]}))["created_at"]}},
        upsert=True,
    )

    calls = {}
    real_tone = _tone(2.0)
    # A genuinely decodable image — ffmpeg needs a real one, not just bytes
    # that start with the PNG signature.
    from PIL import Image

    cover_buf = io.BytesIO()
    Image.new("RGB", (32, 32), (20, 20, 40)).save(cover_buf, format="PNG")
    cover_png = cover_buf.getvalue()

    async def _download(url):
        calls.setdefault("urls", []).append(url)
        return cover_png if "cover" in url else real_tone

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/video",
        json={"end_s": 2.0, "style": "cover", "size": "square"}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    assert any("cover.png" in u for u in calls["urls"])


async def test_make_video_validation(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    url = f"/api/v1/audio-assets/{asset['id']}/video"

    assert (await client.post(url, json={"style": "not-real", "size": "square"}, headers=_h(ws_id))).status_code == 400
    assert (await client.post(url, json={"style": "solid", "size": "not-real"}, headers=_h(ws_id))).status_code == 400
    assert (await client.post(url, json={"start_s": 5, "end_s": 2, "style": "solid", "size": "square"}, headers=_h(ws_id))).status_code == 400
    assert (await client.post("/api/v1/audio-assets/nope/video", json={"style": "solid", "size": "square"}, headers=_h(ws_id))).status_code == 404

    # No duration known and no end_s given: honestly refused, not a 0-length video. (A generated recording now carries its
    # real length, so the length is cleared here to make this the unknown-length case the check is about.)
    await media_assets.update_one({"id": asset["media_id"]}, {"$unset": {"duration_s": ""}})
    no_duration = await client.post(url, json={"style": "solid", "size": "square"}, headers=_h(ws_id))
    assert no_duration.status_code == 400

    doc = await audio_assets.find_one({"id": asset["id"]})
    assert doc.get("video_clips", []) == []  # every refused attempt left no trace


async def test_video_is_refused_over_the_workspaces_size_limit(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset, download = await _with_transcript(client, ws_id, brand_id, seconds=1.0)
    monkeypatch.setattr(audio_module, "_download_media_bytes", download)
    await media_assets.update_one({"id": asset["media_id"]}, {"$set": {"duration_s": 1.0}})
    async def _tiny_limit(kind, ws):
        return 10  # smaller than any real render

    monkeypatch.setattr(audio_module, "_max_bytes_for", _tiny_limit)

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/video",
        json={"end_s": 1.0, "style": "solid", "size": "square"}, headers=_h(ws_id),
    )
    assert res.status_code == 400 and "limit" in res.json()["detail"]


# ── the /suggest-clips endpoint ──────────────────────────────────────────────

async def _asset_with_real_transcript(client, ws_id, brand_id):
    asset = (await _generate(client, ws_id, brand_id)).json()
    words = [{"word": w, "start_s": i * 1.0, "end_s": i * 1.0 + 0.6, "speaker": None}
              for i, w in enumerate(["This", "is", "a", "real", "quote", "worth", "sharing."])]
    await audio_assets.update_one({"id": asset["id"]}, {"$set": {"transcript": words}})
    return asset


async def test_suggest_clips_returns_real_grounded_suggestions(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_real_transcript(client, ws_id, brand_id)

    async def _fake_structured(prompt, **kwargs):
        return {"suggestions": [
            {"start_s": 0.0, "end_s": 4.0, "quote": "This is a real quote", "reason": "A complete thought."},
            {"start_s": 100.0, "end_s": 200.0, "quote": "out of range", "reason": "should be dropped"},
            {"start_s": "bad", "end_s": 2, "quote": "malformed", "reason": "should be dropped"},
        ]}

    monkeypatch.setattr(audio_module, "call_llm_structured", _fake_structured)
    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/suggest-clips", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    suggestions = res.json()["suggestions"]
    assert len(suggestions) == 1  # the out-of-range and malformed ones were filtered out
    assert suggestions[0]["start_s"] == 0.0 and suggestions[0]["end_s"] == 4.0


async def test_suggest_clips_needs_a_transcript(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()  # no transcript
    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/suggest-clips", headers=_h(ws_id))
    assert res.status_code == 400 and "transcript" in res.json()["detail"]


async def test_suggest_clips_surfaces_a_provider_failure_honestly(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_real_transcript(client, ws_id, brand_id)

    async def _boom(prompt, **kwargs):
        raise RuntimeError("groq down")

    monkeypatch.setattr(audio_module, "call_llm_structured", _boom)
    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/suggest-clips", headers=_h(ws_id))
    assert res.status_code == 502
