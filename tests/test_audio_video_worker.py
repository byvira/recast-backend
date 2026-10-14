"""A recording made into a video by the render worker: the edit list for the existing look, the route preferring the worker when it is
there and falling back to the local renderer when it is not. The worker boundary is exercised by writing the job's end state to the
real database, the way the worker's report does; the routes, the edit list, the layer composition and the checks are real."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.api.v1 import audio_assets as audio_module
from app.core.config import settings
from app.db.mongo import audio_assets, get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.pipelines.media.video_render import TranscriptWordLike, _SIZES
from app.video import audio_video, edl
from tests.test_audio_assets import _generate, _h, _setup, stubs  # noqa: F401 - fixture reuse
from tests.test_audio_video import _tone


def _words(n: int = 6, seconds: float = 6.0) -> list[TranscriptWordLike]:
    step = seconds / n
    return [TranscriptWordLike(word=f"w{i}", start_s=i * step, end_s=i * step + 0.3) for i in range(n)]


# ── the edit list for the existing look ──────────────────────────────────────────────────────────────────────────────────

def test_the_edit_has_the_background_the_title_layer_the_voice_and_word_captions():
    edit = audio_video.build_edit(
        size_name="vertical", style="cover", duration_s=3.0, start_s=1.0, words=_words(), accent_hex="#38bdf8",
        background_hex="#0f172a", progress=True, motion=True, has_text_layer=True,
    )
    assert (edit["width"], edit["height"]) == _SIZES["vertical"] and edit["duration_f"] == 90
    assert [c["id"] for c in edit["video"]] == ["bg", "txt"] and edit["video"][1]["alpha"] is True
    assert edit["video"][0]["motion"] is True
    voice = edit["audio"][0]
    assert voice["src_in_f"] == 30 and voice["dur_f"] == 90 and voice["role"] == "voice"
    # caption words are re-timed to the clip: a word at 1.0 s in the recording starts at 0 s in the clip
    first = edit["captions"]["words"][0]
    assert first["s_ms"] == 0 or first["s_ms"] < 400
    assert all(0 <= w["s_ms"] <= w["e_ms"] <= 3000 for w in edit["captions"]["words"])
    assert edit["effects"]["progress_bar"]["enabled"] is True and "waveform" not in edit["effects"]
    assert edl.validate({**{k: v for k, v in edit.items() if k != "effects"}, "video": [], "audio": []}) is not None


def test_a_long_recording_keeps_its_background_still_as_before():
    edit = audio_video.build_edit(
        size_name="landscape", style="solid", duration_s=600.0, start_s=0.0, words=[], accent_hex="#38bdf8",
        background_hex="#0f172a", progress=False, motion=True, has_text_layer=False,
    )
    assert edit["video"][0]["motion"] is False and edit["captions"]["enabled"] is False and "progress_bar" not in edit["effects"]


@pytest.mark.parametrize("style,size", [("waveform", "vertical"), ("waveform", "landscape"), ("cover_wave", "square"), ("cover_wave", "vertical")])
def test_the_wave_sits_where_the_local_renderer_puts_it(style, size):
    w, h = _SIZES[size]
    top, height = audio_video.wave_geometry(style, size, h)
    assert 0.0 <= top < 1.0 and 0.05 <= height <= 0.6 and top + height <= 1.0
    if style == "waveform" and size == "landscape":
        assert abs((top + height / 2) - 0.5) < 0.02            # centred, as before
    edit = audio_video.build_edit(size_name=size, style=style, duration_s=5.0, start_s=0.0, words=[], accent_hex="#FF0000",
                                  background_hex="#000000", progress=False, motion=False, has_text_layer=False)
    assert edit["effects"]["waveform"]["color"] == "#FF0000"


# ── the route ────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def worker_online(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", True)
    monkeypatch.setattr(settings, "VIDEO_WORKER_TOKEN", "t")
    stored: list[str] = []

    async def upload(data, content_type, user_id, filename=None):
        url = f"https://res.cloudinary.com/x/image/upload/v1/{uuid4().hex}.png"
        stored.append(url)
        return url

    removed: list[str] = []

    async def forget(url):
        removed.append(url)
        return True

    monkeypatch.setattr(audio_video, "upload_file", upload)
    monkeypatch.setattr(audio_video, "delete_file_by_url", forget)
    return {"stored": stored, "removed": removed}


async def _worker_finishes(ws: str, *, ok: bool = True, message: str = "") -> str:
    """What the render worker's report does to the job and the library, written to the real database."""
    for _ in range(100):
        job = await get_db()["video_jobs"].find_one({"workspace_id": ws, "status": "queued"})
        if job:
            media_id = str(uuid4())
            if ok:
                await media_assets.insert_one(MediaAsset(
                    id=media_id, workspace_id=ws, kind=MediaKind.VIDEO, url="https://res.cloudinary.com/x/video/upload/v1/out.mp4",
                    mime_type="video/mp4", source=MediaSource.ENHANCED, created_by=job["user_id"], created_at=datetime.now(timezone.utc),
                    size_bytes=1234, duration_s=job["duration_s"], **(job.get("origin") or {}),
                ).model_dump())
                await get_db()["video_jobs"].update_one({"id": job["id"]}, {"$set": {"status": "done", "media_id": media_id}})
            else:
                await get_db()["video_jobs"].update_one({"id": job["id"]}, {"$set": {"status": "failed", "error": message}})
            return job["id"]
        await asyncio.sleep(0.1)
    raise AssertionError("the route never queued a job")


async def test_with_the_worker_online_the_video_is_made_there_and_the_layers_are_cleaned_up(signup_user, stubs, worker_online, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    words = [{"word": f"w{i}", "start_s": i * 0.5, "end_s": i * 0.5 + 0.3, "speaker": None} for i in range(6)]
    await audio_assets.update_one({"id": asset["id"]}, {"$set": {"transcript": words}})
    await media_assets.update_one({"id": asset["media_id"]}, {"$set": {"duration_s": 4.0}})
    await get_db()["video_worker_state"].update_one({"_id": "render_worker"}, {"$set": {"last_seen": datetime.now(timezone.utc)}}, upsert=True)

    async def must_not_download(url):
        raise AssertionError("the worker fetches its own copy; the backend must not download the recording")

    monkeypatch.setattr(audio_module, "_download_media_bytes", must_not_download)
    res, job_id = await asyncio.gather(
        client.post(f"/api/v1/audio-assets/{asset['id']}/video",
                    json={"start_s": 0.0, "end_s": 2.0, "style": "waveform", "size": "square", "title": "Episode"}, headers=_h(ws_id)),
        _worker_finishes(ws_id),
    )
    assert res.status_code == 200, res.text
    clip = res.json()["video_clips"][-1]
    assert clip["style"] == "waveform" and clip["size"] == "square"
    job = await get_db()["video_jobs"].find_one({"id": job_id})
    assert job["edit"]["effects"]["waveform"]["enabled"] is True and job["edit"]["width"] == 1080
    assert job["inputs"]["voice"]["kind"] == "audio" and job["inputs"]["bg"]["kind"] == "image"
    # the made-up layers were stored only for the render and removed after it
    assert sorted(worker_online["removed"]) == sorted(worker_online["stored"]) and worker_online["stored"]
    # the recording is a synthesized narration, so the finished video says it includes AI
    video_media = await media_assets.find_one({"id": clip["media_id"]})
    assert video_media["contains_ai"] is True


async def test_a_failed_render_on_the_worker_is_a_plain_error_and_the_layers_are_still_removed(signup_user, stubs, worker_online):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    await media_assets.update_one({"id": asset["media_id"]}, {"$set": {"duration_s": 4.0}})
    await audio_assets.update_one({"id": asset["id"]}, {"$set": {"transcript": [
        {"word": "hello", "start_s": 0.0, "end_s": 0.3, "speaker": None}]}})
    await get_db()["video_worker_state"].update_one({"_id": "render_worker"}, {"$set": {"last_seen": datetime.now(timezone.utc)}}, upsert=True)
    res, _ = await asyncio.gather(
        client.post(f"/api/v1/audio-assets/{asset['id']}/video", json={"start_s": 0.0, "end_s": 2.0, "style": "solid", "size": "square"},
                    headers=_h(ws_id)),
        _worker_finishes(ws_id, ok=False, message="This recording could not be read."),
    )
    assert res.status_code == 502 and res.json()["detail"] == "This recording could not be read."
    assert sorted(worker_online["removed"]) == sorted(worker_online["stored"])


async def test_with_no_worker_the_same_video_is_made_locally_as_before(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    words = [{"word": f"w{i}", "start_s": i * 0.5, "end_s": i * 0.5 + 0.3, "speaker": None} for i in range(6)]
    await audio_assets.update_one({"id": asset["id"]}, {"$set": {"transcript": words}})
    await media_assets.update_one({"id": asset["media_id"]}, {"$set": {"duration_s": 4.0}})
    real = _tone(4.0)

    async def download(url):
        return real

    monkeypatch.setattr(audio_module, "_download_media_bytes", download)
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", False)
    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/video",
                            json={"start_s": 0.0, "end_s": 2.0, "style": "solid", "size": "square"}, headers=_h(ws_id))
    assert res.status_code == 200, res.text
    clip = res.json()["video_clips"][-1]
    media = await media_assets.find_one({"id": clip["media_id"]})
    assert media["duration_s"] == pytest.approx(2.0, abs=0.05) and media["size_bytes"] > 0
    assert await get_db()["video_jobs"].count_documents({"workspace_id": ws_id}) == 0     # nothing went to the worker
