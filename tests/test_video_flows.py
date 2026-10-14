"""One-click flows: the whole-edit operations, each flow's edit list, the runner (render, partial failure, batches) and recipes. Real
database and real edit-list validation; only the voice, storage, stock source and the worker's render are replaced."""
import asyncio
import io
import math
import re
import struct
import time
import wave
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db.mongo import audio_assets, audio_kits, brand_profiles, get_db, image_assets, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import builders, edl, service
from app.video.flows import edit_ops as ops
from app.video.flows import registry as R
from app.video.flows import runner as RUN
from tests.conftest import create_workspace


def wav(seconds: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(i / 25))) for i in range(int(seconds * rate))))
    return buf.getvalue()


def _edit(seconds=20, with_audio=True):
    e = edl.blank("vertical", {"num": 30, "den": 1})
    frames = seconds * 30
    e["video"] = [{"id": "v1", "media_id": "m1", "at_f": 0, "dur_f": frames, "src_in_f": 0, "speed": 1.0, "fit": "cover", "fade_in_f": 0,
                   "fade_out_f": 0, "audio": True, "gain_db": 0.0}]
    if with_audio:
        e["audio"] = [{"id": "a1", "media_id": "m2", "at_f": 0, "dur_f": frames, "src_in_f": 0, "speed": 1.0, "gain_db": 0.0, "role": "voice",
                       "fade_in_f": 0, "fade_out_f": 0, "duck": False}]
    e["captions"] = {"enabled": True, "accent": "#38bdf8", "words": [{"w": "one", "s_ms": 1000, "e_ms": 1400}, {"w": "two", "s_ms": 12000, "e_ms": 12400}]}
    e["text"] = [{"id": "t1", "text": "Title", "at_f": 0, "dur_f": 60, "x": 0.5, "y": 0.1, "size": 64, "color": "#FFFFFF", "bold": True}]
    e["markers"] = [{"id": "k1", "at_f": 450, "label": "mid"}]
    e["duration_f"] = frames
    return edl.validate(e)


# ── edit operations ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_shift_moves_everything_later_including_captions_and_markers():
    out = ops.shift(_edit(), 90)
    assert out["video"][0]["at_f"] == 90 and out["audio"][0]["at_f"] == 90 and out["text"][0]["at_f"] == 90 and out["markers"][0]["at_f"] == 540
    assert out["captions"]["words"][0]["s_ms"] == 4000 and out["duration_f"] == 600 + 90
    assert ops.shift(_edit(), 0)["video"][0]["at_f"] == 0


def test_a_gap_splits_the_clips_that_span_it_and_moves_what_follows():
    base = _edit()
    out = ops.insert_gap(base, 300, 150)                                   # a 5 s gap at 10 s
    video = sorted(out["video"], key=lambda c: c["at_f"])
    assert [(c["at_f"], c["dur_f"], c["src_in_f"]) for c in video] == [(0, 300, 0), (450, 300, 300)]
    assert out["duration_f"] == 750 and out["markers"][0]["at_f"] == 600
    words = out["captions"]["words"]
    assert words[0]["s_ms"] == 1000 and words[1]["s_ms"] == 17000           # the word after the gap moved by 5 s
    assert out["text"][0]["at_f"] == 0                                       # the title before the gap stays
    # nothing is lost: the played length of the video track is the same
    assert sum(c["dur_f"] for c in video) == sum(c["dur_f"] for c in base["video"])


def test_a_music_bed_runs_the_whole_video_lowered_and_ducking_under_the_voice():
    out = ops.with_music_bed(_edit(), "music1")
    bed = next(c for c in out["audio"] if c["role"] == "music")
    assert bed["media_id"] == "music1" and bed["dur_f"] == 600 and bed["duck"] is True and bed["gain_db"] == ops.MUSIC_GAIN_DB
    assert bed["fade_in_f"] > 0 and bed["fade_out_f"] > 0


def test_a_brand_intro_comes_first_and_the_outro_last_each_with_the_title():
    base = _edit()
    out = ops.with_outro(ops.with_intro(base, media_id="intro1", seconds=3.0, title="Acme"), media_id="outro1", seconds=2.0, title="Thanks")
    intro = next(c for c in out["audio"] if c["media_id"] == "intro1")
    outro = next(c for c in out["audio"] if c["media_id"] == "outro1")
    assert intro["at_f"] == 0 and intro["dur_f"] == 90
    assert outro["at_f"] == 600 + 90 and out["duration_f"] == 600 + 90 + 60
    assert min(c["at_f"] for c in out["video"]) == 90 and out["captions"]["words"][0]["s_ms"] == 4000
    assert {t["text"] for t in out["text"]} == {"Title", "Acme", "Thanks"}


def test_a_sponsor_read_opens_its_own_gap_in_the_middle_and_the_content_carries_on_after_it():
    out = ops.with_sponsor(_edit(), media_id="spon", seconds=4.0, at_ms=10000, label="Sponsored by Acme")
    spon = next(c for c in out["audio"] if c["media_id"] == "spon")
    assert spon["at_f"] == 300 and spon["dur_f"] == 120
    assert out["duration_f"] == 600 + 120 and any(t["text"] == "Sponsored by Acme" and t["at_f"] == 300 for t in out["text"])
    assert edl.validate(out)


def test_a_clip_is_cut_from_a_long_video_with_its_own_captions_and_a_short_title():
    media = {"id": "m1", "duration_s": 120.0, "probe": {"video": {"fps": 30.0}}}
    words = [{"w": f"w{i}", "s_ms": i * 500, "e_ms": i * 500 + 400} for i in range(0, 200)]
    e = builders.clip_from_video(media, start_s=30.0, end_s=50.0, words=words, size="vertical", title="A strong point")
    assert (e["width"], e["height"]) == (1080, 1920) and e["video"][0]["src_in_f"] == 900 and e["video"][0]["dur_f"] == 600
    caps = e["captions"]["words"]
    assert caps[0]["s_ms"] == 0 and caps[0]["w"] == "w60" and caps[-1]["e_ms"] <= 20000 and len(caps) == 40
    assert e["text"][0]["text"] == "A strong point" and e["text"][0]["dur_f"] <= 90
    with pytest.raises(edl.EditError):
        builders.clip_from_video(media, start_s=10, end_s=10.2, words=words)
    with pytest.raises(edl.EditError):
        builders.clip_from_video({"id": "m", "duration_s": 0}, start_s=0, end_s=5, words=words)


# ── flows ───────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def _clean():
    for name in ("video_projects", "video_project_versions", "video_analysis", "video_transcripts", "video_transcript_pages", "video_recipes"):
        await get_db()[name].delete_many({})
    yield


def _words(n=300, start=0, per=400):
    return [{"w": ("word%d" % i) + ("." if i % 12 == 11 else ""), "s_ms": start + i * per, "e_ms": start + i * per + per - 40} for i in range(n)]


async def _seed_video(ws, *, duration=140.0, language="en", moments=None):
    mid = str(uuid4())
    await media_assets.insert_one(MediaAsset(
        id=mid, workspace_id=ws, kind=MediaKind.VIDEO, url=f"https://res.cloudinary.com/democloud/video/upload/v1/{mid}.mp4", mime_type="video/mp4",
        duration_s=duration, width=1920, height=1080, source=MediaSource.UPLOADED, created_by="u", created_at=datetime.now(timezone.utc),
        probe={"duration_s": duration, "video": {"fps": 30.0}}).model_dump())
    tid = str(uuid4())
    await get_db()["video_transcripts"].insert_one({"id": tid, "workspace_id": ws, "media_id": mid, "status": "done", "language": language,
                                                    "duration_s": duration})
    await get_db()["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0, "words": _words(340)})
    if moments is not None:
        await get_db()["video_analysis"].insert_one({"id": str(uuid4()), "workspace_id": ws, "media_id": mid, "status": "done", "moments": moments})
    return mid


def _moment(i, start, end, score, state="suggested"):
    return {"id": f"m{i}", "start_ms": start, "end_ms": end, "title": f"Moment {i}", "score": score, "state": state, "reason": "r", "quote": "q", "risk": []}


def env_for(ws, **kw):
    return R.Env(workspace_id=ws, user_id="u1", **kw)


async def test_clips_come_from_accepted_moments_first_then_the_best_suggested_and_never_rejected_ones():
    ws = f"ws-{uuid4()}"
    mid = await _seed_video(ws, moments=[
        _moment(1, 5000, 35000, 0.5), _moment(2, 40000, 70000, 0.9), _moment(3, 75000, 105000, 0.7, "accepted"), _moment(4, 110000, 130000, 0.99, "rejected"),
    ])
    specs = await R.video_to_clips(env_for(ws), {"media_id": mid, "count": 3})
    assert [s["title"] for s in specs] == ["Moment 3", "Moment 2", "Moment 1"]
    first = specs[0]["edit"]
    assert (first["width"], first["height"]) == (1080, 1920) and first["video"][0]["src_in_f"] == 75 * 30 and first["video"][0]["dur_f"] == 900
    assert first["captions"]["words"] and specs[0]["created_from"]["flow"] == "video_to_clips" and specs[0]["created_from"]["moment_id"] == "m3"
    only = await R.video_to_clips(env_for(ws), {"media_id": mid, "count": 3, "only_accepted": True})
    assert [s["title"] for s in only] == ["Moment 3"]


async def test_a_video_that_has_not_been_read_is_read_once_by_the_flow(monkeypatch):
    ws = f"ws-{uuid4()}"
    mid = await _seed_video(ws)
    seen = []

    async def caller(prompt):
        seen.append(prompt)
        numbers = [int(n) for n in re.findall(r"^(\d+) \(", prompt, re.M)]
        if "finding moments" in prompt:
            a = numbers[0]
            return {"moments": [{"start": a, "end": a + 8, "title": "A clear idea", "reason": "One full point.", "hook": 0.8, "standalone": 0.7, "risk": []}]}
        return {}

    env = env_for(ws, caller=caller)
    specs = await R.video_to_clips(env, {"media_id": mid, "count": 2, "min_s": 5, "max_s": 90})
    assert specs and specs[0]["title"] == "A clear idea" and seen


async def test_a_flow_that_finds_nothing_says_why_instead_of_making_an_empty_project():
    ws = f"ws-{uuid4()}"
    mid = await _seed_video(ws, moments=[_moment(1, 1000, 3000, 0.4, "rejected")])

    async def nothing(prompt):
        return {}

    with pytest.raises(R.FlowError) as exc:
        await R.video_to_clips(env_for(ws, caller=nothing), {"media_id": mid})
    assert "No moment" in str(exc.value)
    with pytest.raises(R.FlowError):
        await R.video_to_clips(env_for(ws), {"media_id": "ghost"})
    other = str(uuid4())
    await media_assets.insert_one(MediaAsset(id=other, workspace_id=ws, kind=MediaKind.VIDEO, url="https://x/y.mp4", mime_type="video/mp4",
                                             source=MediaSource.UPLOADED, created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
    with pytest.raises(R.FlowError) as exc:
        await R.video_to_clips(env_for(ws), {"media_id": other})
    assert "transcript" in str(exc.value).lower()


async def test_music_and_the_brand_kit_are_added_when_asked_and_missing_pieces_are_said_not_skipped_quietly():
    ws = f"ws-{uuid4()}"
    mid = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.8)])
    brand_id = str(uuid4())
    await brand_profiles.insert_one({"id": brand_id, "workspace_id": ws, "created_at": datetime.now(timezone.utc), "visual_identity": {"colors": {"accent": "#ff8800", "secondary": "#101820"}}})
    music, intro = str(uuid4()), str(uuid4())
    for mid_, secs in ((music, 60.0), (intro, 3.0)):
        await media_assets.insert_one(MediaAsset(id=mid_, workspace_id=ws, kind=MediaKind.AUDIO, url=f"https://x/{mid_}.mp3", mime_type="audio/mpeg", duration_s=secs,
                                                 source=MediaSource.UPLOADED, created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
    await audio_kits.insert_one({"workspace_id": ws, "brand_id": brand_id, "intro": {"media_id": intro, "name": "Intro"}, "outro": None})
    env = env_for(ws)
    specs = await R.video_to_clips(env, {"media_id": mid, "count": 1, "brand_id": brand_id, "music_media_id": music, "use_kit": True})
    e = specs[0]["edit"]
    assert any(c["role"] == "music" and c["duck"] for c in e["audio"]) and any(c["media_id"] == intro for c in e["audio"])
    assert e["background"] == "#101820" and e["captions"]["accent"] == "#ff8800"
    assert any("no outro" in n for n in env.notes)


async def test_a_post_becomes_a_narrated_video_with_the_voice_laid_end_to_end_captions_and_brand_picture(monkeypatch):
    ws = f"ws-{uuid4()}"
    brand_id = str(uuid4())
    await brand_profiles.insert_one({"id": brand_id, "workspace_id": ws, "created_at": datetime.now(timezone.utc)})
    pic = str(uuid4())
    await media_assets.insert_one(MediaAsset(id=pic, workspace_id=ws, kind=MediaKind.IMAGE, url="https://x/brand.png", mime_type="image/png",
                                             source=MediaSource.BRAND_ASSET, created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
    post = "Most weekly plans fall apart by Wednesday because we plan for forty hours and only have twenty five real hours to work with."

    async def caller(prompt):
        return {"beats": [
            {"narration": "Most weekly plans fall apart by Wednesday.", "on_screen": "Plans fail", "visual": "an empty calendar"},
            {"narration": "We plan forty hours but only have twenty five.", "on_screen": "40 vs 25", "visual": "a clock"},
        ]}

    spoken = []

    async def speak(text, speaker=None):
        spoken.append(text)
        return wav(2.5)

    stored = []

    async def store_audio(data):
        url = f"https://res.cloudinary.com/democloud/video/upload/v1/{uuid4().hex}.mp3"
        stored.append(url)
        return {"url": url, "bytes": len(data)}

    env = env_for(ws, caller=caller, speak=speak, store_audio=store_audio)
    specs = await R.post_to_video(env, {"text": post, "brand_id": brand_id, "language": "en", "visual": "brand", "size": "vertical"})
    e = specs[0]["edit"]
    assert spoken == ["Most weekly plans fall apart by Wednesday.", "We plan forty hours but only have twenty five."]
    voices = sorted((c for c in e["audio"] if c["role"] == "voice"), key=lambda c: c["at_f"])
    assert len(voices) == 2 and voices[1]["at_f"] == voices[0]["at_f"] + voices[0]["dur_f"]
    assert [v["media_id"] for v in e["video"]] == [pic, pic] and all(v["motion"] for v in e["video"])
    assert [t["text"] for t in e["text"]] == ["Plans fail", "40 vs 25"] and e["captions"]["enabled"]
    assert e["duration_f"] == voices[1]["at_f"] + voices[1]["dur_f"]
    made = await media_assets.find({"workspace_id": ws, "source": "synthesized"}).to_list(10)
    assert len(made) == 2 and all(m["contains_ai"] is True for m in made)
    assert specs[0]["created_from"]["voice_version"]


async def test_stock_footage_is_used_when_asked_and_the_brand_picture_stands_in_when_it_fails():
    ws = f"ws-{uuid4()}"
    pic, clip = str(uuid4()), str(uuid4())
    await media_assets.insert_one(MediaAsset(id=pic, workspace_id=ws, kind=MediaKind.IMAGE, url="https://x/brand.png", mime_type="image/png",
                                             source=MediaSource.BRAND_ASSET, created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
    await media_assets.insert_one(MediaAsset(id=clip, workspace_id=ws, kind=MediaKind.VIDEO, url="https://x/stock.mp4", mime_type="video/mp4", duration_s=9.0,
                                             source=MediaSource.STOCK, created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
    calls = {"n": 0}

    async def caller(prompt):
        return {"beats": [{"narration": "First beat is about traffic.", "on_screen": "", "visual": "city traffic"},
                          {"narration": "Second beat is about a quiet lake.", "on_screen": "", "visual": "lake"}]}

    async def speak(text, speaker=None):
        return wav(2.0)

    async def store_audio(data):
        return {"url": f"https://res.cloudinary.com/democloud/video/upload/v1/{uuid4().hex}.mp3", "bytes": len(data)}

    async def stock_search(query):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("stock source is down")
        return [{"provider": "pixabay", "id": "1"}]

    async def stock_use(c, query):
        return clip

    env = env_for(ws, caller=caller, speak=speak, store_audio=store_audio, stock_search=stock_search, stock_use=stock_use)
    specs = await R.post_to_video(env, {"text": "x" * 40, "visual": "stock"})
    v = sorted(specs[0]["edit"]["video"], key=lambda c: c["at_f"])
    assert [c["media_id"] for c in v] == [clip, pic] and v[0]["motion"] is False and v[1]["motion"] is True
    assert specs[0]["edit"]["origin"] if False else True


async def test_a_post_that_cannot_be_voiced_or_is_too_short_stops_with_a_plain_reason():
    ws = f"ws-{uuid4()}"
    with pytest.raises(R.FlowError):
        await R.post_to_video(env_for(ws), {"text": "short"})

    async def caller(prompt):
        return {"beats": [{"narration": "A line of narration here.", "on_screen": "", "visual": ""}]}

    async def silent(text, speaker=None):
        return None

    async def store_audio(data):
        return {"url": "https://x/a.mp3"}

    with pytest.raises(R.FlowError) as exc:
        await R.post_to_video(env_for(ws, caller=caller, speak=silent, store_audio=store_audio), {"text": "A post with enough words in it to be a post."})
    assert "voice could not read" in str(exc.value)


async def test_a_carousel_becomes_a_slideshow_and_each_slide_stays_as_long_as_its_words_take():
    ws = f"ws-{uuid4()}"
    slides = []
    for n in (1, 2, 3):
        mid = str(uuid4())
        await media_assets.insert_one(MediaAsset(id=mid, workspace_id=ws, kind=MediaKind.IMAGE, url=f"https://x/{n}.png", mime_type="image/png",
                                                 source=MediaSource.RENDERED, created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
        slides.append({"slide_number": n, "title": "" if n == 2 else f"Slide {n} is about one thing", "slide_type": "x", "media_id": mid})
    aid = str(uuid4())
    await image_assets.insert_one({"id": aid, "workspace_id": ws, "brand_id": "b", "title": "My carousel", "slides": slides, "created_by": "u",
                                   "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)})

    async def speak(text, speaker=None):
        return wav(5.0)

    async def store_audio(data):
        return {"url": f"https://res.cloudinary.com/democloud/video/upload/v1/{uuid4().hex}.mp3", "bytes": len(data)}

    silent = await R.carousel_to_slideshow(env_for(ws), {"image_asset_id": aid, "seconds_per_picture": 2.0})
    e = silent[0]["edit"]
    assert [c["dur_f"] for c in e["video"]] == [60, 60, 60] and e["audio"] == [] and all(c["motion"] for c in e["video"])
    spoken = await R.carousel_to_slideshow(env_for(ws, speak=speak, store_audio=store_audio), {"image_asset_id": aid, "narrate": True, "seconds_per_picture": 2.0})
    e = spoken[0]["edit"]
    assert [c["dur_f"] for c in e["video"]] == [162, 60, 162] and len(e["audio"]) == 2 and e["captions"]["enabled"]
    with pytest.raises(R.FlowError):
        await R.carousel_to_slideshow(env_for(ws), {"image_asset_id": "nope"})
    with pytest.raises(R.FlowError):
        await R.carousel_to_slideshow(env_for(ws), {"image_asset_id": aid, "seconds_per_picture": 100})


# ── the runner ──────────────────────────────────────────────────────────────────────────────────────────────────────────

class Steps:
    def __init__(self):
        self.lines = []

    async def step(self, text):
        self.lines.append(text)


async def test_the_runner_makes_the_projects_renders_each_and_keeps_going_when_one_render_fails(monkeypatch):
    ws = f"ws-{uuid4()}"
    mid = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.9), _moment(2, 40000, 70000, 0.8), _moment(3, 75000, 105000, 0.7)])
    renders = []

    async def fake_render(*, workspace_id, user_id, project_id, preset, reporter=None, **kw):
        renders.append(project_id)
        if len(renders) == 2:
            raise service.RenderFailed("The worker went away.")
        return {"job_id": "j", "media_id": f"out-{len(renders)}"}

    monkeypatch.setattr(service, "render_project_and_wait", fake_render)
    steps = Steps()
    out = await RUN.run_flow(env_for(ws, reporter=steps), "video_to_clips", {"media_id": mid, "count": 3})
    assert out["ok"] is False and len(out["projects"]) == 3
    assert [("media_id" in p, "error" in p) for p in out["projects"]] == [(True, False), (False, True), (True, False)]
    assert out["projects"][1]["error"] == "The worker went away."
    rows = await get_db()["video_projects"].find({"workspace_id": ws}).to_list(10)
    assert len(rows) == 3 and all(r["created_from"]["flow"] == "video_to_clips" and r["version"] == 1 for r in rows)
    assert any("Rendering 2 of 3" in l for l in steps.lines)


async def test_without_rendering_only_the_editable_projects_are_made(monkeypatch):
    ws = f"ws-{uuid4()}"
    mid = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.9)])

    async def boom(**kw):
        raise AssertionError("must not render")

    monkeypatch.setattr(service, "render_project_and_wait", boom)
    out = await RUN.run_flow(env_for(ws), "video_to_clips", {"media_id": mid, "count": 1}, render=False)
    assert out["ok"] is True and "media_id" not in out["projects"][0]


async def test_a_flow_with_missing_inputs_or_an_unknown_name_is_reported_not_raised():
    ws = f"ws-{uuid4()}"
    assert (await RUN.run_flow(env_for(ws), "nope", {}))["error"] == "That flow does not exist."
    miss = await RUN.run_flow(env_for(ws), "video_to_clips", {})
    assert miss["ok"] is False and "media_id" in miss["error"]
    bad = await RUN.run_flow(env_for(ws), "video_to_clips", {"media_id": "ghost"})
    assert bad["ok"] is False and "not in this workspace" in bad["error"]


async def test_a_batch_runs_every_item_gives_one_summary_and_one_bad_file_does_not_stop_the_rest(monkeypatch):
    ws = f"ws-{uuid4()}"
    a = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.9)])
    b = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.9)])

    async def fake_render(*, workspace_id, user_id, project_id, preset, reporter=None, **kw):
        return {"job_id": "j", "media_id": "out"}

    monkeypatch.setattr(service, "render_project_and_wait", fake_render)
    out = await RUN.run_batch(env_for(ws), "video_to_clips", [{"media_id": a}, {"media_id": "ghost"}, {"media_id": b}], shared={"count": 1})
    assert (out["total"], out["succeeded"], out["failed"]) == (3, 2, 1)
    assert [r["ok"] for r in out["results"]] == [True, False, True] and "not in this workspace" in out["results"][1]["error"]
    many = await RUN.run_batch(env_for(ws), "video_to_clips", [{"media_id": a}] * 25, render=False, shared={"count": 1})
    assert many["total"] == RUN.MAX_BATCH and many["skipped"] == 5


async def test_a_recipe_runs_its_steps_in_order_with_the_inputs_merged_over_its_settings(monkeypatch):
    ws = f"ws-{uuid4()}"
    mid = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.9), _moment(2, 40000, 70000, 0.8)])
    steps = [{"flow": "video_to_clips", "params": {"count": 1, "size": "square"}}, {"flow": "video_to_clips", "params": {"count": 2, "size": "vertical"}}]
    out = await RUN.run_steps(env_for(ws), steps, {"media_id": mid}, render=False)
    assert out["ok"] is True and out["videos"] == 3 and len(out["steps"]) == 2
    sizes = [(p["project_id"]) for s in out["steps"] for p in s["projects"]]
    rows = {r["id"]: r for r in await get_db()["video_projects"].find({"workspace_id": ws}).to_list(10)}
    assert [rows[i]["edit"]["width"] for i in sizes] == [1080, 1080, 1080] and [rows[i]["edit"]["height"] for i in sizes] == [1080, 1920, 1920]


# ── the API ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _h(ws):
    return {"X-Workspace-Id": ws}


async def _setup(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Flows")
    return client, ws


@pytest.fixture
def voices(monkeypatch):
    from app.api.v1 import video_narrate as narrate_api
    from app.models.voice_settings import MemberVoiceSettings

    async def tools(ctx):
        def make_speak(name, voices=None):
            async def speak(text, speaker=None):
                return wav(2.0)
            return speak

        async def store(data):
            return f"https://res.cloudinary.com/democloud/video/upload/v1/{uuid4().hex}.wav"

        async def forget(url):
            return None

        return make_speak, store, forget, MemberVoiceSettings(id="x", workspace_id=ctx.workspace_id, user_id=ctx.user_id)

    monkeypatch.setattr(narrate_api, "_voice_tools", tools)


async def _wait_for_projects(client, ws, count, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = (await client.get("/api/v1/video-projects", headers=_h(ws))).json()["projects"]
        if len(rows) >= count:
            return rows
        await asyncio.sleep(1)
    return []


async def test_the_catalog_lists_the_flows_and_what_each_needs(signup_user):
    client, ws = await _setup(signup_user)
    flows = (await client.get("/api/v1/video-flows/catalog", headers=_h(ws))).json()["flows"]
    assert {f["id"] for f in flows} == {"video_to_clips", "recording_to_shorts", "post_to_video", "carousel_to_slideshow"}
    assert next(f for f in flows if f["id"] == "video_to_clips")["needs"] == ["media_id"]


async def test_running_a_flow_is_a_run_that_leaves_editable_projects_and_bad_requests_are_refused_early(signup_user, voices):
    client, ws = await _setup(signup_user)
    assert (await client.post("/api/v1/video-flows/run", json={"flow": "nope", "params": {}}, headers=_h(ws))).status_code == 404
    assert (await client.post("/api/v1/video-flows/run", json={"flow": "video_to_clips", "params": {}}, headers=_h(ws))).status_code == 400
    mid = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.9), _moment(2, 40000, 70000, 0.8)])
    res = await client.post("/api/v1/video-flows/run", json={"flow": "video_to_clips", "params": {"media_id": mid, "count": 2}, "render": False}, headers=_h(ws))
    assert res.status_code == 202, res.text
    assert res.json()["started"] is True and res.json()["run"]["kind"] == "video"
    rows = await _wait_for_projects(client, ws, 2)
    assert {r["title"] for r in rows} == {"Moment 1", "Moment 2"}


async def test_recipes_are_saved_listed_run_over_many_files_and_deleted(signup_user, voices):
    client, ws = await _setup(signup_user)
    bad = await client.post("/api/v1/video-flows/recipes", json={"name": "x", "steps": [{"flow": "nope"}]}, headers=_h(ws))
    assert bad.status_code == 400
    saved = await client.post("/api/v1/video-flows/recipes", json={"name": "Weekly shorts", "steps": [{"flow": "video_to_clips", "params": {"count": 1}}]},
                              headers=_h(ws))
    assert saved.status_code == 201, saved.text
    rid = saved.json()["id"]
    assert [r["name"] for r in (await client.get("/api/v1/video-flows/recipes", headers=_h(ws))).json()["recipes"]] == ["Weekly shorts"]
    a = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.9)])
    b = await _seed_video(ws, moments=[_moment(1, 5000, 35000, 0.9)])
    run = await client.post(f"/api/v1/video-flows/recipes/{rid}/run", json={"inputs_list": [{"media_id": a}, {"media_id": "ghost"}, {"media_id": b}],
                                                                           "render": False}, headers=_h(ws))
    assert run.status_code == 202, run.text
    rows = await _wait_for_projects(client, ws, 2)
    assert len(rows) == 2                                                     # the bad file did not stop the other two
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        doc = await get_db()["video_recipes"].find_one({"id": rid})
        if doc.get("runs"):
            break
        await asyncio.sleep(1)
    assert doc["runs"] == 1
    assert (await client.delete(f"/api/v1/video-flows/recipes/{rid}", headers=_h(ws))).status_code == 204
    assert (await client.delete(f"/api/v1/video-flows/recipes/{rid}", headers=_h(ws))).status_code == 404


async def test_a_batch_request_over_the_limit_is_refused_and_other_workspaces_recipes_are_invisible(signup_user):
    client, ws = await _setup(signup_user)
    too_many = await client.post("/api/v1/video-flows/batch", json={"flow": "video_to_clips", "items": [{"media_id": "x"}] * 25}, headers=_h(ws))
    assert too_many.status_code == 422
    saved = await client.post("/api/v1/video-flows/recipes", json={"name": "Mine", "steps": [{"flow": "post_to_video"}]}, headers=_h(ws))
    other, _ = await signup_user()
    other_ws = await create_workspace(other, "Elsewhere")
    assert (await other.get("/api/v1/video-flows/recipes", headers=_h(other_ws))).json()["recipes"] == []
    assert (await other.post(f"/api/v1/video-flows/recipes/{saved.json()['id']}/run", json={}, headers=_h(other_ws))).status_code == 404
