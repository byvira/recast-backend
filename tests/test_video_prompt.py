"""A video made from a description: the scenes builder, the draft (script and stock suggestions, nothing saved or spoken), and the make step
(clips saved, scenes spoken, a project opened). Only the script model, the stock library and the voice are replaced."""
import asyncio
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.api.v1 import video_narrate as narrate_api
from app.api.v1 import video_prompt as prompt_api
from app.db.mongo import get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import builders, edl
from app.video.stock import providers
from app.video.stock import service as stock
from tests.conftest import create_workspace
from tests.test_video_narrate import Harness


def _h(ws):
    return {"X-Workspace-Id": ws}


# ── the builder (no database) ────────────────────────────────────────────────────

def _beat(n, spoken=(2.0,), clip_s=None, on_screen=""):
    return {"text": f"Scene {n} says something worth hearing", "on_screen": on_screen,
            "voice": [{"media_id": f"voice{n}_{k}", "seconds": s} for k, s in enumerate(spoken)],
            "clip": {"media_id": f"clip{n}", "seconds": clip_s} if clip_s else None}


def test_scenes_follow_each_other_with_their_voice_and_footage():
    edit = builders.from_beats(beats=[_beat(1, (2.0,), 5.0, "First"), _beat(2, (1.0, 1.5), 10.0)], size="vertical")
    assert (edit["width"], edit["height"]) == (1080, 1920)
    voices = edit["audio"]
    assert [a["media_id"] for a in voices] == ["voice1_0", "voice2_0", "voice2_1"]
    assert voices[1]["at_f"] == voices[0]["at_f"] + voices[0]["dur_f"] + round(0.35 * 30)  # a short breath between scenes
    assert voices[2]["at_f"] == voices[1]["at_f"] + voices[1]["dur_f"]
    assert [c["media_id"] for c in edit["video"]] == ["clip1", "clip2"]
    assert edit["video"][1]["at_f"] == edit["video"][0]["dur_f"]
    assert edit["text"][0]["text"] == "First" and edit["captions"]["enabled"] and edit["captions"]["words"]
    assert edit["duration_f"] == edit["video"][1]["at_f"] + edit["video"][1]["dur_f"]


def test_short_footage_repeats_to_fill_the_scene_and_long_footage_is_cut_to_it():
    short = builders.from_beats(beats=[_beat(1, (6.0,), 2.0)])
    clips = short["video"]
    assert len(clips) >= 3 and all(c["media_id"] == "clip1" for c in clips)
    assert clips[-1]["at_f"] + clips[-1]["dur_f"] == short["duration_f"]
    long_ = builders.from_beats(beats=[_beat(1, (2.0,), 30.0)])
    assert len(long_["video"]) == 1 and long_["video"][0]["dur_f"] == long_["duration_f"]


def test_a_scene_without_footage_still_has_its_voice_and_the_captions_can_be_off():
    edit = builders.from_beats(beats=[_beat(1, (2.0,), None)], captions=False)
    assert edit["video"] == [] and len(edit["audio"]) == 1 and edit["captions"]["enabled"] is False


def test_nothing_to_make_or_a_silent_scene_is_refused():
    with pytest.raises(edl.EditError):
        builders.from_beats(beats=[])
    with pytest.raises(edl.EditError):
        builders.from_beats(beats=[{"text": "x", "voice": [], "clip": None}])
    with pytest.raises(edl.EditError):
        builders.from_beats(beats=[_beat(n) for n in range(builders.MAX_BEATS + 1)])


# ── the routes ───────────────────────────────────────────────────────────────────

SCRIPT = {"beats": [
    {"narration": "Plan your week in ten minutes.", "on_screen": "Ten minutes", "visual": "person writing a plan", "seconds": 3.0},
    {"narration": "Start with the one thing that matters most.", "on_screen": "", "visual": "sunrise over a desk", "seconds": 4.0},
], "total_seconds": 7.0, "target_seconds": 45}


def _clip(i, seconds=6.0):
    return {"provider": "pixabay", "id": str(i), "page_url": f"https://pixabay.com/v/{i}", "download_url": f"https://cdn.example/{i}.mp4",
            "preview_url": f"https://cdn.example/{i}-s.mp4", "poster_url": None, "width": 1080, "height": 1920, "duration_s": seconds,
            "author": "Maker", "author_url": "", "license": "Pixabay", "license_url": "", "tags": ["plan"]}


@pytest.fixture
def fakes(monkeypatch):
    """No real model, stock library or voice."""
    calls = {"script": 0, "search": 0, "download": 0}
    h = Harness()

    async def script(source, *, target_seconds, language, voice, caller=None):
        calls["script"] += 1
        return SCRIPT, {"prompt_version": "t", "model_slot": "t", "tries": 1, "language": language, "problems": []}

    monkeypatch.setattr(prompt_api.G, "script_from_text", script)
    monkeypatch.setattr(providers, "configured", lambda: ["pixabay"])

    async def search(query, *, providers=None, per_page=12):
        calls["search"] += 1
        return {"clips": [_clip(1), _clip(2), _clip(3, 1.0)], "cached": False, "errors": []}

    monkeypatch.setattr(providers, "search", search)

    async def use_clip(*, workspace_id, user_id, provider, clip_id, query="", fetch=None, get_one=None):
        calls["download"] += 1
        media = MediaAsset(
            id=str(uuid4()), workspace_id=workspace_id, kind=MediaKind.VIDEO, url=f"https://res.cloudinary.com/democloud/video/upload/v1/{clip_id}.mp4",
            mime_type="video/mp4", duration_s=6.0, source=MediaSource.STOCK, created_by=user_id, created_at=datetime.now(timezone.utc),
            stock={"provider": provider, "id": clip_id, "author": "Maker", "license": "Pixabay"},
        )
        await media_assets.insert_one(media.model_dump())
        return media

    monkeypatch.setattr(stock, "use_clip", use_clip)

    from app.models.voice_settings import MemberVoiceSettings

    async def tools(ctx):
        def make_speak(name, voices=None):
            async def speak(text, speaker=None):
                return await h.speak(text, speaker)
            return speak
        return make_speak, h.store, h.forget, MemberVoiceSettings(id="x", workspace_id=ctx.workspace_id, user_id=ctx.user_id)

    monkeypatch.setattr(narrate_api, "_voice_tools", tools)
    import app.pipelines.media.tts_generation as tts
    monkeypatch.setattr(tts, "narration_blocker", lambda language, settings_obj: None)
    return calls, h


async def _setup(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Prompt video", tier="large")
    return client, ws


async def test_a_draft_has_scenes_with_ranked_suggestions_and_saves_and_speaks_nothing(signup_user, fakes):
    calls, h = fakes
    client, ws = await _setup(signup_user)
    before = await media_assets.count_documents({"workspace_id": ws})
    res = await client.post("/api/v1/video-prompt/draft", json={"prompt": "A calm way to plan your week", "size": "vertical", "target_seconds": 45}, headers=_h(ws))
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["scenes"]) == 2 and body["connected"] is True and body["note"] is None
    assert 1 <= len(body["scenes"][0]["options"]) <= 4 and "download_url" not in body["scenes"][0]["options"][0]
    assert calls["download"] == 0 and h.spoken == []
    assert await media_assets.count_documents({"workspace_id": ws}) == before


async def test_without_a_stock_library_the_draft_says_so_and_still_has_the_words(signup_user, fakes, monkeypatch):
    client, ws = await _setup(signup_user)
    monkeypatch.setattr(providers, "configured", lambda: [])
    res = await client.post("/api/v1/video-prompt/draft", json={"prompt": "A calm way to plan your week"}, headers=_h(ws))
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is False and "No stock library" in body["note"] and all(s["options"] == [] for s in body["scenes"])


async def test_a_script_with_no_scenes_is_an_honest_error(signup_user, fakes, monkeypatch):
    client, ws = await _setup(signup_user)

    async def empty(source, *, target_seconds, language, voice, caller=None):
        return {"beats": [], "total_seconds": 0}, {"prompt_version": "t", "model_slot": "t", "tries": 2, "language": "en", "problems": ["The script has no beats."]}

    monkeypatch.setattr(prompt_api.G, "script_from_text", empty)
    res = await client.post("/api/v1/video-prompt/draft", json={"prompt": "A calm way to plan your week"}, headers=_h(ws))
    assert res.status_code == 502 and "Try describing" in res.json()["detail"]


async def test_a_short_description_is_refused(signup_user, fakes):
    client, ws = await _setup(signup_user)
    res = await client.post("/api/v1/video-prompt/draft", json={"prompt": "hi"}, headers=_h(ws))
    assert res.status_code == 422


async def _wait(client, ws, run_id):
    deadline = time.monotonic() + 90
    while True:
        run = (await client.get(f"/api/v1/runs/{run_id}", headers=_h(ws))).json()
        if run.get("status") not in ("running", "queued", "pending") or time.monotonic() > deadline:
            return run
        await asyncio.sleep(0.5)


async def test_making_the_video_saves_the_clips_speaks_each_scene_and_opens_a_project(signup_user, fakes):
    calls, h = fakes
    client, ws = await _setup(signup_user)
    body = {"scenes": [
        {"narration": "Plan your week in ten minutes.", "on_screen": "Ten minutes", "clip": {"provider": "pixabay", "id": "1"}},
        {"narration": "Start with the one thing that matters most.", "clip": None},
    ], "size": "vertical", "title": "Weekly plan", "prompt": "A calm way to plan your week"}
    started = await client.post("/api/v1/video-prompt/make", json=body, headers=_h(ws))
    assert started.status_code == 202, started.text
    run = await _wait(client, ws, started.json()["run"]["id"])
    assert run["status"] == "done", run
    project_id = run["result"]["data"]["project_id"] if "result" in run else run["data"]["project_id"]
    assert calls["download"] == 1 and len(h.spoken) == 2
    proj = (await client.get(f"/api/v1/video-projects/{project_id}", headers=_h(ws))).json()
    assert proj["title"] == "Weekly plan" and len(proj["edit"]["audio"]) == 2 and len(proj["edit"]["video"]) >= 1
    assert proj["origin"]["origin"] in ("ai", "mixed") or proj["origin"].get("contains_ai")
    assert h.forgotten == []  # the voice files belong to the library now


async def test_a_voice_that_cannot_read_the_language_stops_before_anything_is_spent(signup_user, fakes, monkeypatch):
    calls, h = fakes
    client, ws = await _setup(signup_user)
    import app.pipelines.media.tts_generation as tts
    monkeypatch.setattr(tts, "narration_blocker", lambda language, settings_obj: "Tamil narration needs the premium voice service.")
    res = await client.post("/api/v1/video-prompt/make", json={"scenes": [{"narration": "Hello there", "clip": None}], "language": "Tamil"}, headers=_h(ws))
    assert res.status_code == 409 and "premium voice" in res.json()["detail"]
    assert calls["download"] == 0 and h.spoken == []


async def test_a_failed_voice_removes_what_the_run_had_saved(signup_user, fakes, monkeypatch):
    calls, h = fakes
    client, ws = await _setup(signup_user)
    original = h.speak
    count = {"n": 0}

    async def flaky(text, speaker=None):
        count["n"] += 1
        return await original(text, speaker) if count["n"] == 1 else None

    monkeypatch.setattr(h, "speak", flaky)
    before = await media_assets.count_documents({"workspace_id": ws, "kind": "audio"})
    started = await client.post("/api/v1/video-prompt/make", json={"scenes": [
        {"narration": "Plan your week.", "clip": None}, {"narration": "Start with one thing.", "clip": None}]}, headers=_h(ws))
    assert started.status_code == 202
    run = await _wait(client, ws, started.json()["run"]["id"])
    assert run["status"] in ("failed", "error"), run
    assert await media_assets.count_documents({"workspace_id": ws, "kind": "audio"}) == before
    assert len(h.forgotten) == 1
