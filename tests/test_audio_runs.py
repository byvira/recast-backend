"""Tests for live step tracking: a long audio operation reports which step it
is on while it runs, and the report disappears when it ends, however it ends.
The step is read from inside the operation itself (a stubbed provider call
asks "where am I?" mid-request), so this proves it is visible while running,
not just afterwards.
"""
from uuid import uuid4

from app.api.v1 import audio_assets as audio_module
from app.pipelines.media.tts_generation import SpeechResult
from app.db.mongo import media_assets
from app.shared.activity.runs import list_runs
from tests.test_audio_assets import _generate, _h, _setup, _wav, stubs  # noqa: F401 — fixture reuse


async def test_generate_reports_its_step_while_running_and_clears_after(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    run_id = "run-" + uuid4().hex
    seen: dict = {}

    async def _synth_that_looks_around(*, text, voice_settings, lexicon=None, workspace_id, user_id, language=None):
        mid_request = await client.get(f"/api/v1/audio-assets/runs/{run_id}", headers=_h(ws_id))
        seen["status"] = mid_request.status_code
        seen["body"] = mid_request.json()
        seen["listed"] = [r for r in await list_runs(ws_id) if r["id"] == run_id]
        return SpeechResult(audio=_wav(0.4), words=None)

    monkeypatch.setattr(audio_module, "synthesize_speech_timed", _synth_that_looks_around)
    res = await client.post(
        "/api/v1/audio-assets/generate",
        json={"title": "Ep", "brand_id": brand_id, "script": "Hello there."},
        headers={**_h(ws_id), "X-Run-Id": run_id},
    )
    assert res.status_code == 201, res.text

    assert seen["status"] == 200
    assert seen["body"] == {"stage": "Voicing your script", "steps_done": 0, "steps_total": 2, "title": "Narration"}
    assert seen["listed"] and seen["listed"][0]["kind"] == "audio"  # shows up in the Activity popover too

    # Finished: nothing left to report.
    assert (await client.get(f"/api/v1/audio-assets/runs/{run_id}", headers=_h(ws_id))).status_code == 404


async def test_a_failed_operation_still_clears_its_run(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    run_id = "run-" + uuid4().hex
    stubs["tts_returns_none"] = True

    res = await client.post(
        "/api/v1/audio-assets/generate",
        json={"title": "Ep", "brand_id": brand_id, "script": "Hello."},
        headers={**_h(ws_id), "X-Run-Id": run_id},
    )
    assert res.status_code == 503
    assert (await client.get(f"/api/v1/audio-assets/runs/{run_id}", headers=_h(ws_id))).status_code == 404
    assert [r for r in await list_runs(ws_id) if r["kind"] == "audio"] == []


async def test_upload_and_cleanup_report_their_own_steps(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)

    # Upload: the transcription call runs on step 3 of 4.
    upload_run = "run-" + uuid4().hex
    seen: dict = {}

    async def _transcribe_that_looks_around(data, *, filename):
        seen["upload"] = (await client.get(f"/api/v1/audio-assets/runs/{upload_run}", headers=_h(ws_id))).json()
        return []

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes", _transcribe_that_looks_around)
    res = await client.post(
        "/api/v1/audio-assets/upload",
        data={"title": "Take", "brand_id": brand_id},
        files={"file": ("take.wav", _wav(0.6, noisy=True), "audio/wav")},
        headers={**_h(ws_id), "X-Run-Id": upload_run},
    )
    assert res.status_code == 201, res.text
    assert seen["upload"]["stage"] == "Transcribing what was said"
    assert seen["upload"]["steps_done"] == 2 and seen["upload"]["steps_total"] == 4
    asset = res.json()

    # Cleanup: the download is the first step of 3.
    cleanup_run = "run-" + uuid4().hex

    async def _download_that_looks_around(url):
        seen["cleanup"] = (await client.get(f"/api/v1/audio-assets/runs/{cleanup_run}", headers=_h(ws_id))).json()
        return _wav(0.6, noisy=True)

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download_that_looks_around)
    cleaned = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/cleanup", json={"compressor": 0.5},
        headers={**_h(ws_id), "X-Run-Id": cleanup_run},
    )
    assert cleaned.status_code == 200, cleaned.text
    assert seen["cleanup"]["stage"] == "Loading the recording" and seen["cleanup"]["steps_total"] == 3


async def test_run_ids_are_validated_and_workspace_scoped(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    other, _, other_ws, _ = await _setup(signup_user)
    run_id = "run-" + uuid4().hex
    seen: dict = {}

    async def _synth(*, text, voice_settings, lexicon=None, workspace_id, user_id, language=None):
        seen["other_workspace"] = (await other.get(f"/api/v1/audio-assets/runs/{run_id}", headers=_h(other_ws))).status_code
        seen["by_bad_id"] = (await client.get("/api/v1/audio-assets/runs/x", headers=_h(ws_id))).status_code
        return SpeechResult(audio=_wav(0.3), words=None)

    monkeypatch.setattr(audio_module, "synthesize_speech_timed", _synth)
    # A header that isn't a plausible id is ignored: the server picks its own.
    res = await client.post(
        "/api/v1/audio-assets/generate",
        json={"title": "Ep", "brand_id": brand_id, "script": "Hi."},
        headers={**_h(ws_id), "X-Run-Id": "not valid!!"},
    )
    assert res.status_code == 201, res.text
    assert seen["by_bad_id"] == 404

    # A valid id in another workspace is invisible.
    res2 = await client.post(
        "/api/v1/audio-assets/generate",
        json={"title": "Ep2", "brand_id": brand_id, "script": "Hi."},
        headers={**_h(ws_id), "X-Run-Id": run_id},
    )
    assert res2.status_code == 201
    assert seen["other_workspace"] == 404
    assert await media_assets.count_documents({"workspace_id": ws_id}) >= 2
