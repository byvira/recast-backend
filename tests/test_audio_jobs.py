"""The audio actions as background jobs: each starts at once through /jobs, reports its steps (also to the live tracker the Audio page
reads), keeps going after the request, and ends with the asset it made. The voice, transcription and upload providers are stubbed."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import audio_assets, get_db
from app.models.audio_asset import TranscriptWord
from app.shared import job_actions, jobs
from tests.test_audio_assets import _generate, _h, _setup, _upload, _wav, stubs  # noqa: F401 - fixture reuse
from tests.test_audio_cleanup import _uploaded_with_transcript, cleanup_source  # noqa: F401 - fixture reuse
from tests.test_pipeline_runs import _wait_for


async def _start(client, headers, action, payload, **extra_headers):
    res = await client.post("/api/v1/jobs", json={"action": action, "payload": payload}, headers={**headers, **extra_headers})
    assert res.status_code == 202, res.text
    return res.json()


async def test_the_audio_actions_are_registered_and_only_safe_ones_start_again_after_a_restart():
    jobs.ACTIONS.clear()
    job_actions.register_all()
    audio = {name: a for name, a in jobs.ACTIONS.items() if name.startswith("audio.")}
    assert set(audio) == {"audio.transcribe", "audio.cleanup", "audio.assemble", "audio.video", "audio.regenerate", "audio.import", "audio.dialogue"}
    assert {n for n, a in audio.items() if a.restartable} == {"audio.transcribe", "audio.assemble", "audio.video", "audio.import"}
    assert all(a.kind == "audio" for a in audio.values())


async def test_transcribe_in_the_background_ends_with_the_assets_words(signup_user, stubs, monkeypatch):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    audio = _wav(0.5)

    async def _nothing(data, *, filename):
        return []

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes", _nothing)
    asset = (await _upload(client, ws_id, brand_id, audio)).json()
    assert asset["transcript"] == []

    async def _words(data, *, filename):
        return [TranscriptWord(word="hello", start_s=0.0, end_s=0.3)]

    async def _download(url):
        return audio

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes", _words)
    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)

    run = await _start(client, _h(ws_id), "audio.transcribe", {"audio_asset_id": asset["id"]})
    assert run["kind"] == "audio" and run["title"] == "Transcribe recording"
    done = await _wait_for(client, run["id"], ("done", "failed"), headers=_h(ws_id))

    assert done["status"] == "done", done
    assert done["result"]["data"]["asset_id"] == asset["id"]
    assert done["href"] == f"/dashboard/pipelines/audio?asset={asset['id']}"
    stored = await audio_assets.find_one({"id": asset["id"]})
    assert [w["word"] for w in stored["transcript"]] == ["hello"]


async def test_cleanup_in_the_background_makes_a_new_version(signup_user, stubs, cleanup_source):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _uploaded_with_transcript(client, ws_id, brand_id)

    run = await _start(client, _h(ws_id), "audio.cleanup", {"audio_asset_id": asset["id"], "settings": {"silence_trim_s": 0.5, "target_lufs": -16}})
    done = await _wait_for(client, run["id"], ("done", "failed"), seconds=90, headers=_h(ws_id))

    assert done["status"] == "done", done
    assert done["result"]["data"]["version_count"] == 2
    versions = (await client.get(f"/api/v1/audio-assets/{asset['id']}/versions", headers=_h(ws_id))).json()["versions"]
    assert [v["action"] for v in versions] == ["created", "cleanup"]


async def test_voicing_a_script_again_in_the_background_is_a_new_version(signup_user, stubs):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    run = await _start(client, _h(ws_id), "audio.regenerate", {"audio_asset_id": asset["id"], "settings": {"script": "A new script."}})
    done = await _wait_for(client, run["id"], ("done", "failed"), seconds=60, headers=_h(ws_id))

    assert done["status"] == "done", done
    assert done["result"]["data"] == {"asset_id": asset["id"], "version_count": 2}
    assert (await audio_assets.find_one({"id": asset["id"]}))["script"] == "A new script."


async def test_a_failure_in_the_work_is_recorded_with_its_reason(signup_user, stubs):  # noqa: F811
    client, _, ws_id, _ = await _setup(signup_user)

    run = await _start(client, _h(ws_id), "audio.transcribe", {"audio_asset_id": "does-not-exist"})
    done = await _wait_for(client, run["id"], ("done", "failed"), headers=_h(ws_id))

    assert done["status"] == "failed"
    assert "not found" in done["error"].lower()


async def test_an_upload_can_run_in_the_background_and_ends_with_the_new_recording(signup_user, stubs):  # noqa: F811
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await client.post(
        "/api/v1/audio-assets/upload/background", data={"title": "Raw take", "brand_id": brand_id},
        files={"file": ("take.wav", _wav(1.0, noisy=True), "audio/wav")}, headers=_h(ws_id),
    )

    assert res.status_code == 202, res.text
    run = res.json()
    assert run["kind"] == "audio" and run["title"] == "Upload: Raw take"
    done = await _wait_for(client, run["id"], ("done", "failed"), seconds=90, headers=_h(ws_id))
    assert done["status"] == "done", done
    asset = (await client.get(f"/api/v1/audio-assets/{done['result']['data']['asset_id']}", headers=_h(ws_id))).json()
    assert asset["source_type"] == "uploaded" and asset["title"] == "Raw take"
    assert done["href"] == f"/dashboard/pipelines/audio?asset={asset['id']}"


async def test_a_viewer_cannot_start_an_audio_job(signup_user):
    client, profile, ws_id, _ = await _setup(signup_user)
    ctx = await jobs.build_context(ws_id, profile["id"])
    ctx.member = {**ctx.member, "role": "viewer"}
    for action, payload in (("audio.cleanup", {"audio_asset_id": "a", "settings": {"target_lufs": -16}}), ("audio.import", {"settings": {"brand_id": "b", "url": "https://example.com/a.mp3"}})):
        with pytest.raises(HTTPException) as caught:
            await jobs.submit(action_name=action, payload=payload, ctx=ctx)
        assert caught.value.status_code == 403, action


async def test_steps_are_reported_to_the_saved_run_and_to_the_live_tracker(monkeypatch):
    seen = []

    async def update_run(workspace_id, run_id, **fields):
        seen.append((workspace_id, run_id, fields))

    monkeypatch.setattr("app.shared.activity.runs.update_run", update_run)
    checkpoints = []

    class Base:
        async def step(self, label):
            checkpoints.append(label)

    reporter = job_actions._AudioReporter(Base(), "w1", "live-run-12345")
    await reporter.step("Fetching the file")
    await reporter.step("Saving the audio")

    assert checkpoints == ["Fetching the file", "Saving the audio"]
    assert [(r, f["stage"], f["steps_done"]) for _, r, f in seen] == [("live-run-12345", "Fetching the file", 0), ("live-run-12345", "Saving the audio", 1)]


async def test_an_invalid_live_id_is_ignored_and_never_reaches_the_tracker(monkeypatch):
    seen = []

    async def update_run(*args, **fields):
        seen.append(fields)

    monkeypatch.setattr("app.shared.activity.runs.update_run", update_run)

    class Base:
        async def step(self, label):
            return None

    for bad in (None, "", "short", "has spaces in it!", "x" * 80):
        await job_actions._AudioReporter(Base(), "w1", bad).step("Step")
    assert seen == []


async def test_the_video_guard_records_the_render_and_a_second_start_is_refused():
    ws_id, asset_id = "ws-guard-1", "asset-guard-1"
    ctx = SimpleNamespace(workspace_id=ws_id)
    body = audio_module.MakeVideoRequest()
    release = asyncio.Event()

    async def slow_render():
        await release.wait()
        return "rendered"

    first = asyncio.create_task(job_actions._with_video_guard(audio_module._video_render_guard(asset_id, body, ctx), slow_render()))
    await asyncio.sleep(0.2)
    with pytest.raises(HTTPException) as caught:
        await job_actions._with_video_guard(audio_module._video_render_guard(asset_id, body, ctx), slow_render())
    assert caught.value.status_code == 409

    release.set()
    assert await first == "rendered"
    jobs_rows = await get_db()["audio_render_jobs"].find({"workspace_id": ws_id, "audio_asset_id": asset_id}).to_list(length=5)
    assert [r["status"] for r in jobs_rows] == ["done"]


async def test_a_failed_render_is_recorded_as_failed_and_the_error_still_reaches_the_caller():
    ws_id, asset_id = "ws-guard-2", "asset-guard-2"
    ctx = SimpleNamespace(workspace_id=ws_id)

    async def broken():
        raise HTTPException(status_code=400, detail="The recording is too short.")

    with pytest.raises(HTTPException) as caught:
        await job_actions._with_video_guard(audio_module._video_render_guard(asset_id, audio_module.MakeVideoRequest(), ctx), broken())
    assert caught.value.detail == "The recording is too short."
    rows = await get_db()["audio_render_jobs"].find({"workspace_id": ws_id, "audio_asset_id": asset_id}).to_list(length=5)
    assert [(r["status"], r["error"]) for r in rows] == [("failed", "The recording is too short.")]
