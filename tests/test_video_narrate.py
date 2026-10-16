"""Narration and dub: timing rules, translation fitting, the edit lists, the whole flow on the real queue with only the voice, storage
and worker wait replaced, and the API."""
import asyncio
import io
import math
import struct
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.db.mongo import get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import jobs, service
from app.video.create import generate as G
from app.video.narrate import dub as D
from app.video.narrate import plan as P
from app.video.narrate import service as N
from app.api.v1 import video_narrate as narrate_api
from tests.conftest import create_workspace


def wav(seconds: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 20))) for i in range(n)))
    return buf.getvalue()


def _words(sentences: list[str], gap_ms: int = 700, per_word_ms: int = 350) -> list[dict]:
    out, t = [], 0
    for s in sentences:
        for w in s.split():
            out.append({"w": w, "s_ms": t, "e_ms": t + per_word_ms - 30})
            t += per_word_ms
        t += gap_ms
    return out


def echo_lines(prompt: str, prefix: str = "ES") -> list[tuple[int, str]]:
    """(line number, source text) for every numbered line in a dub prompt."""
    import re

    return [(int(m.group(1)), m.group(2)) for m in re.finditer(r"^(\d+) \([\d.]+ s\) (.*)$", prompt, re.M)]


S3 = ["Welcome to the show today.", "We will look at how teams plan a calmer week and why the plan matters so much.",
      "Plan for twenty five real hours, not forty.", "Thanks for watching and see you next time."]


# ── segments and fitting ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_spoken_lines_come_from_sentences_with_short_ones_joined_and_their_available_time_known():
    segs = P.segments_from_words(_words(S3))
    assert [s["i"] for s in segs] == list(range(1, len(segs) + 1)) and len(segs) >= 2
    assert all(s["slot_s"] > 0 and s["room_s"] >= s["slot_s"] for s in segs)
    assert segs[0]["text"].startswith("Welcome to the show today.") and all(a["start_ms"] < b["start_ms"] for a, b in zip(segs, segs[1:]))
    # nothing is lost: every original word is in exactly one line
    assert " ".join(s["text"] for s in segs).split() == " ".join(S3).split()


def test_a_translation_is_judged_by_words_against_the_seconds_the_original_had():
    assert P.fit_status("one two three four five", 4.0, "en")["status"] == "ok"
    long = " ".join(["word"] * 15)
    assert P.fit_status(long, 4.0, "en")["status"] == "too_long"
    near = " ".join(["word"] * 11)                               # 4.4 s of speech in 4 s: a small speed-up
    assert P.fit_status(near, 4.0, "en")["status"] == "speed_up"
    assert P.words_per_second("ta") < P.words_per_second("en")


def test_placement_speeds_a_line_up_at_most_a_little_flags_what_overruns_and_never_overlaps():
    segs = [{"i": 1, "start_ms": 0, "end_ms": 4000, "text": "a", "slot_s": 4.0, "room_s": 4.5},
            {"i": 2, "start_ms": 5000, "end_ms": 8000, "text": "b", "slot_s": 3.0, "room_s": 3.4},
            {"i": 3, "start_ms": 9000, "end_ms": 12000, "text": "c", "slot_s": 3.0, "room_s": 3.4},
            {"i": 4, "start_ms": 13000, "end_ms": 15000, "text": "d", "slot_s": 2.0, "room_s": 2.5}]
    placed = P.place(segs, [3.5, 6.0, 3.1, None])
    assert placed[0]["flag"] == "ok" and placed[0]["speed"] == 1.0 and placed[0]["at_ms"] == 0
    assert placed[1]["flag"] == "overruns" and placed[1]["speed"] == P.MAX_SPEEDUP                         # 6 s into a 3 s slot
    assert placed[2]["flag"] == "ok" or placed[2]["flag"] == "sped_up"
    assert placed[3]["flag"] == "missing" and placed[3]["dur_ms"] == 0
    for a, b in zip(placed, placed[1:]):
        if b["dur_ms"]:
            assert a["at_ms"] + a["dur_ms"] <= b["at_ms"]
    assert P.summary(placed)["needs_review"] == [2, 4]


def test_a_late_line_is_pushed_after_the_one_before_it_not_laid_over_it():
    segs = [{"i": 1, "start_ms": 0, "end_ms": 2000, "text": "a", "slot_s": 2.0, "room_s": 2.0},
            {"i": 2, "start_ms": 2200, "end_ms": 4000, "text": "b", "slot_s": 1.8, "room_s": 1.9}]
    placed = P.place(segs, [5.0, 1.0])
    assert placed[1]["at_ms"] >= placed[0]["at_ms"] + placed[0]["dur_ms"] and placed[1]["late_ms"] > 0


def test_caption_words_follow_the_spoken_lines_own_length():
    words = P.caption_words("Planifica con las horas reales", 10000, 3000)
    assert [w["w"] for w in words] == ["Planifica", "con", "las", "horas", "reales"]
    assert words[0]["s_ms"] == 10000 and words[-1]["e_ms"] <= 13000
    assert all(a["e_ms"] <= b["s_ms"] for a, b in zip(words, words[1:])) and words[0]["e_ms"] - words[0]["s_ms"] > words[1]["e_ms"] - words[1]["s_ms"]
    assert P.caption_words("", 0, 1000) == [] and P.caption_words("hi", 0, 0) == []


def test_the_dub_and_narration_edit_lists_are_valid_and_lower_the_original_voice():
    placed = [{"i": 1, "at_ms": 0, "dur_ms": 3000, "speed": 1.0, "text_out": "Hola a todos", "flag": "ok"},
              {"i": 2, "at_ms": 4000, "dur_ms": 2000, "speed": 1.1, "text_out": "Gracias", "flag": "sped_up"},
              {"i": 3, "at_ms": 7000, "dur_ms": 0, "speed": 1.0, "text_out": "", "flag": "missing"}]
    edit = P.build_dub_edit(width=1280, height=720, duration_s=10.0, placed=placed)
    assert edit["video"][0]["gain_db"] == P.ORIGINAL_DUCK_DB and edit["video"][0]["audio"] is True
    assert [a["id"] for a in edit["audio"]] == ["d1", "d2"] and edit["audio"][1]["speed"] == 1.1 and edit["audio"][1]["at_f"] == 120
    assert edit["captions"]["enabled"] and len(edit["captions"]["words"]) == 4
    inputs = {"src": {"url": "https://x/a.mp4", "kind": "video"}, "d1": {"url": "https://x/1.wav", "kind": "audio"},
              "d2": {"url": "https://x/2.wav", "kind": "audio"}}
    assert jobs._validate_request(inputs, edit) > 0
    nar = P.build_narration_edit(width=1280, height=720, duration_s=8.0, narration_seconds=11.0, has_music=True)
    assert nar["duration_f"] == 330 and nar["video"][0]["dur_f"] == 240 and nar["audio"][1]["duck"] is True
    assert jobs._validate_request({"src": {"url": "https://x/a.mp4", "kind": "video"}, "voice": {"url": "https://x/v.wav", "kind": "audio"},
                                   "music": {"url": "https://x/m.mp3", "kind": "audio"}},
                                  {**nar, "audio": [{**nar["audio"][0], "src": "voice"}, nar["audio"][1] | {"src": "music"}]}) > 0


def test_the_worker_accepts_the_edit_lists_made_here():
    worker_src = Path(__file__).resolve().parents[3] / "Worker" / "ekavo-video-worker" / "src"
    if not worker_src.exists():
        pytest.skip("the worker repo is not next to the backend")
    sys.path.insert(0, str(worker_src))
    try:
        from ekavo_worker.contract import parse_job
    finally:
        sys.path.remove(str(worker_src))
    placed = [{"i": 1, "at_ms": 0, "dur_ms": 3000, "speed": 1.0, "text_out": "Hola", "flag": "ok"},
              {"i": 2, "at_ms": 4000, "dur_ms": 2000, "speed": 1.12, "text_out": "Gracias", "flag": "sped_up"}]
    edit = P.build_dub_edit(width=1280, height=720, duration_s=10.0, placed=placed)
    raw = {"id": "dub-1", "kind": "render", "contract": 1, "lease_token": "l", "lease_seconds": 60, "edit": edit,
           "inputs": {"src": {"url": "https://x/a.mp4", "kind": "video"}, "d1": {"url": "https://x/1.wav", "kind": "audio"},
                      "d2": {"url": "https://x/2.wav", "kind": "audio"}}}
    job = parse_job(raw)
    assert [a.id for a in job.edit.audio] == ["d1", "d2"] and job.edit.audio[1].speed == pytest.approx(1.12)


# ── translating ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_dub_answer_must_hold_every_line_keep_terms_and_figures_and_not_be_cut_short():
    sources = ["Ekavo makes 12 plans a week.", "Thanks for watching today everybody.", "See you next time."]
    raw = {"lines": [{"i": 1, "text": "Ekavo hace planes cada semana."},                 # drops the figure 12
                     {"i": 2, "text": "Gracias por ver hoy a todos."},
                     {"i": 7, "text": "fuera"}]}
    out, problems = D.validate_dub(raw, 3, sources, ["Ekavo"])
    assert out == {2: "Gracias por ver hoy a todos."}
    text = " ".join(problems)
    assert "figure" in text and "Line 7" in text and "Line 3 is missing" in text
    kept, problems = D.validate_dub({"lines": [{"i": 1, "text": "hace 12 planes a la semana."}]}, 1, ["Ekavo makes 12 plans a week."], ["Ekavo"])
    assert kept == {} and "Ekavo" in problems[0]


async def test_translation_runs_in_batches_asks_once_more_for_lines_that_are_too_long_and_lists_what_failed():
    segs = [{"i": n, "start_ms": n * 5000, "end_ms": n * 5000 + 3000, "text": f"Line number {n} is here today", "slot_s": 3.0, "room_s": 4.0}
            for n in range(1, 26)]
    seen: list[str] = []

    async def caller(prompt):
        seen.append(prompt)
        rows = echo_lines(prompt)
        if "clearly shorter" in prompt:
            return {"lines": [{"i": k, "text": f"Corta {src.split()[2]} aqui"} for k, src in rows]}
        out = []
        for k, src in rows:
            number = src.split()[2]
            if number == "3":
                continue                                                   # never answered
            words = 14 if number == "5" else 5                              # line 5 is far too long for 3 seconds
            out.append({"i": k, "text": " ".join(["palabra"] * words) + f" numero {number}"})
        return {"lines": out}

    out, rep = await D.translate(segs, target_name="Spanish", target_code="es", caller=caller)
    assert len(out) == 25 and rep["failed"] == [3]
    five = next(x for x in out if x["i"] == 5)
    assert five["tightened"] is True and five["text"] == "Corta 5 aqui" and five["fit"]["status"] == "ok"
    assert rep["tightened"] == [5]
    assert sum(1 for p in seen if "clearly shorter" in p) == 1
    assert sum(1 for p in seen if "Return exactly 20 lines" in p) >= 1 and sum(1 for p in seen if "Return exactly 5 lines" in p) >= 1


async def test_a_shorter_rewrite_that_is_not_shorter_is_not_used():
    segs = [{"i": 1, "start_ms": 0, "end_ms": 3000, "text": "A fairly long original line here today", "slot_s": 3.0, "room_s": 3.5}]
    calls = []

    async def caller(prompt):
        calls.append(prompt)
        long = " ".join(["palabra"] * 14)
        return {"lines": [{"i": 1, "text": long if len(calls) == 1 else long + " mas mas"}]}

    out, rep = await D.translate(segs, target_name="Spanish", target_code="es", caller=caller)
    assert out[0]["tightened"] is False and out[0]["text"] == " ".join(["palabra"] * 14) and out[0]["fit"]["status"] == "too_long"


def test_round_trip_similarity_counts_words_heard_in_order():
    assert D.roundtrip_similarity("one two three four", "one two three four") == 1.0
    assert D.roundtrip_similarity("one two three four", "one three four") == 0.75
    assert D.roundtrip_similarity("one two three four", "four three two one") < 0.5
    assert D.roundtrip_similarity("", "anything") == 1.0


# ── the whole flow on the real queue ─────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def _clean():
    for name in ("video_jobs", "video_dubs", "video_caption_tracks", "video_transcripts", "video_transcript_pages"):
        await get_db()[name].delete_many({})
    yield


@pytest.fixture
def cloud(monkeypatch):
    from app.core.config import settings

    for k, v in (("CLOUDINARY_CLOUD_NAME", "democloud"), ("CLOUDINARY_API_KEY", "1234"), ("CLOUDINARY_API_SECRET", "shhh")):
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", True)


async def _video(ws, *, duration=60.0):
    mid = str(uuid4())
    row = MediaAsset(id=mid, workspace_id=ws, kind=MediaKind.VIDEO, url=f"https://res.cloudinary.com/democloud/video/upload/v1/{uuid4()}.mp4",
                     mime_type="video/mp4", duration_s=duration, width=1280, height=720, source=MediaSource.UPLOADED, created_by="u",
                     created_at=datetime.now(timezone.utc)).model_dump()
    await media_assets.insert_one(dict(row))
    return row


class Harness:
    def __init__(self, lengths=None):
        self.spoken: list[tuple[str, str | None]] = []
        self.stored: list[str] = []
        self.forgotten: list[str] = []
        self.lengths = lengths or {}

    async def speak(self, text, speaker=None):
        self.spoken.append((text, speaker))
        return wav(self.lengths.get(text, 2.0))

    async def store(self, data):
        url = f"https://res.cloudinary.com/democloud/video/upload/v1/recast/tmp/{uuid4().hex}.wav"
        self.stored.append(url)
        return url

    async def forget(self, url):
        self.forgotten.append(url)


@pytest.fixture
def finish(monkeypatch):
    """The worker's part: the render ends and its video appears in the library."""
    seen = {}

    async def wait_for_job(workspace_id, job_id, *, reporter=None, max_wait_s=None):
        job = await get_db()["video_jobs"].find_one({"id": job_id})
        seen["job"] = job
        out = str(uuid4())
        await media_assets.insert_one(MediaAsset(
            id=out, workspace_id=workspace_id, kind=MediaKind.VIDEO, url=f"https://res.cloudinary.com/democloud/video/upload/v1/{out}.mp4",
            mime_type="video/mp4", duration_s=60.0, source=MediaSource.ENHANCED, created_by="u", created_at=datetime.now(timezone.utc),
            **{k: (job.get("origin") or {}).get(k) for k in ("origin", "contains_ai", "contains_stock")}).model_dump())
        return {"job_id": job_id, "media_id": out}

    monkeypatch.setattr(service, "wait_for_job", wait_for_job)
    return seen


async def test_a_dub_is_translated_spoken_placed_and_rendered_with_the_original_lowered_and_temporary_files_removed(cloud, finish):
    ws = f"ws-{uuid4()}"
    media = await _video(ws)
    h = Harness()

    async def model(prompt):
        return {"lines": [{"i": k, "text": "ES " + src} for k, src in echo_lines(prompt)]}

    result = await N.make_dub(
        workspace_id=ws, user_id="u1", media=media, words=_words(S3), target_code="es", target_name="Spanish", speak=h.speak, store=h.store,
        forget=h.forget, caller=lambda p: model(p), glossary=[],
    )
    job = finish["job"]
    assert set(job["inputs"]) == {"src"} | {f"d{i}" for i in range(1, len(result["lines"]) + 1)}
    assert job["edit"]["video"][0]["gain_db"] == P.ORIGINAL_DUCK_DB
    assert len(job["edit"]["audio"]) == len(result["lines"]) and job["edit"]["captions"]["enabled"]
    assert job["origin"]["origin"] == "mixed" and job["origin"]["contains_ai"] is True
    assert sorted(h.forgotten) == sorted(h.stored) and len(h.stored) == len(result["lines"])
    assert result["summary"]["lines"] == len(result["lines"]) and result["language"] == "es"
    out = await media_assets.find_one({"id": result["media_id"]})
    assert out["contains_ai"] is True


async def test_too_many_untranslated_lines_stop_the_dub_before_any_voice_is_paid_for(cloud, finish):
    ws = f"ws-{uuid4()}"
    media = await _video(ws)
    h = Harness()

    async def broken(prompt):
        return {}

    with pytest.raises(service.RenderFailed) as exc:
        await N.make_dub(workspace_id=ws, user_id="u1", media=media, words=_words(S3), target_code="es", target_name="Spanish",
                         speak=h.speak, store=h.store, forget=h.forget, caller=broken)
    assert "could not be translated" in str(exc.value) and h.spoken == [] and h.stored == []


async def test_temporary_files_are_removed_even_when_the_render_fails(cloud, monkeypatch):
    ws = f"ws-{uuid4()}"
    media = await _video(ws)
    h = Harness()

    async def model(prompt):
        return {"lines": [{"i": k, "text": "ES " + src} for k, src in echo_lines(prompt)]}

    async def failing(*a, **k):
        raise service.RenderFailed("The worker went away.")

    monkeypatch.setattr(service, "wait_for_job", failing)
    with pytest.raises(service.RenderFailed):
        await N.make_dub(workspace_id=ws, user_id="u1", media=media, words=_words(S3), target_code="es", target_name="Spanish", speak=h.speak,
                         store=h.store, forget=h.forget, caller=model)
    assert h.stored and sorted(h.forgotten) == sorted(h.stored)


async def test_a_video_over_the_length_limit_and_a_video_with_no_speech_are_refused_plainly(cloud, finish):
    ws = f"ws-{uuid4()}"
    h = Harness()
    long = await _video(ws, duration=10 ** 6)
    with pytest.raises(service.RenderFailed) as exc:
        await N.make_dub(workspace_id=ws, user_id="u1", media=long, words=_words(S3), target_code="es", target_name="Spanish",
                         speak=h.speak, store=h.store, forget=h.forget)
    assert "limit" in str(exc.value)
    with pytest.raises(service.RenderFailed) as exc:
        await N.make_dub(workspace_id=ws, user_id="u1", media=await _video(ws), words=[], target_code="es", target_name="Spanish",
                         speak=h.speak, store=h.store, forget=h.forget)
    assert "no speech" in str(exc.value)


# ── narration ────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_a_script_is_spoken_in_parts_and_laid_end_to_end_with_captions_and_a_ducked_music_bed(cloud, finish):
    ws = f"ws-{uuid4()}"
    media = await _video(ws, duration=30.0)
    h = Harness()
    script = " ".join(f"Sentence number {i} of the narration." for i in range(1, 400))
    result = await N.make_narration(workspace_id=ws, user_id="u1", media=media, script=script, speak=h.speak, store=h.store, forget=h.forget,
                                    music_url="https://res.cloudinary.com/democloud/video/upload/v1/m.mp3")
    job = finish["job"]
    clips = [a for a in job["edit"]["audio"] if a["role"] == "voice"]
    assert result["parts"] == len(clips) > 1
    assert all(b["at_f"] == a["at_f"] + a["dur_f"] for a, b in zip(clips, clips[1:]))
    music = next(a for a in job["edit"]["audio"] if a["role"] == "music")
    assert music["duck"] is True and "music" in job["inputs"]
    assert job["edit"]["duration_f"] >= clips[-1]["at_f"] + clips[-1]["dur_f"] and sorted(h.forgotten) == sorted(h.stored)
    assert job["origin"]["contains_ai"] is True


async def test_fit_to_video_refuses_a_narration_that_runs_far_past_the_video(cloud, finish):
    ws = f"ws-{uuid4()}"
    media = await _video(ws, duration=10.0)
    h = Harness(lengths={"A line that runs long.": 30.0})
    with pytest.raises(service.RenderFailed) as exc:
        await N.make_narration(workspace_id=ws, user_id="u1", media=media, script="A line that runs long.", speak=h.speak, store=h.store,
                               forget=h.forget, fit_to_video=True)
    assert "Shorten the script" in str(exc.value) and h.stored == []


async def test_each_speaker_of_a_dialogue_is_spoken_in_their_own_voice(cloud, finish):
    ws = f"ws-{uuid4()}"
    media = await _video(ws, duration=20.0)
    h = Harness()
    await N.make_narration(workspace_id=ws, user_id="u1", media=media, speak=h.speak, store=h.store, forget=h.forget,
                           lines=[{"speaker": "host", "text": "Welcome back."}, {"speaker": "guest", "text": "Glad to be here."},
                                  {"speaker": "host", "text": "Let us begin."}])
    assert h.spoken == [("Welcome back.", "host"), ("Glad to be here.", "guest"), ("Let us begin.", "host")]


# ── the API ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _h(ws):
    return {"X-Workspace-Id": ws}


async def _setup(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Voices")
    return client, ws


async def _with_transcript(ws, language="en", sentences=S3):
    media = await _video(ws)
    tid = str(uuid4())
    await get_db()["video_transcripts"].insert_one({"id": tid, "workspace_id": ws, "media_id": media["id"], "status": "done", "language": language,
                                                    "duration_s": 60.0})
    await get_db()["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0, "words": _words(sentences)})
    return media, tid


@pytest.fixture
def voices(monkeypatch):
    """No real voice provider: a fake speaker, storage and settings, and a model that answers per line."""
    h = Harness()
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

    answers = {"mode": "ok"}

    def slot(name):
        async def call(prompt):
            return {"lines": [{"i": k, "text": "Linea traducida " + src} for k, src in echo_lines(prompt)]}
        return call

    monkeypatch.setattr(G, "slot_caller", slot)
    return h


async def test_dubbing_needs_a_transcript_a_different_language_and_a_voice_that_can_read_it(signup_user, voices, monkeypatch):
    client, ws = await _setup(signup_user)
    media = await _video(ws)
    res = await client.post("/api/v1/video-narrate/dub", json={"media_id": media["id"], "language": "Spanish"}, headers=_h(ws))
    assert res.status_code == 409 and "transcript" in res.json()["detail"].lower()
    media, _ = await _with_transcript(ws, language="en")
    same = await client.post("/api/v1/video-narrate/dub", json={"media_id": media["id"], "language": "English"}, headers=_h(ws))
    assert same.status_code == 400 and "already spoken" in same.json()["detail"]
    import app.pipelines.media.tts_generation as tts
    monkeypatch.setattr(tts, "narration_blocker", lambda language, settings_obj: "Tamil narration needs the premium voice service.")
    blocked = await client.post("/api/v1/video-narrate/dub", json={"media_id": media["id"], "language": "Tamil"}, headers=_h(ws))
    assert blocked.status_code == 409 and "premium voice" in blocked.json()["detail"]
    assert (await client.post("/api/v1/video-narrate/dub", json={"media_id": "nope", "language": "Spanish"}, headers=_h(ws))).status_code == 404


async def test_a_dub_runs_as_a_run_lists_its_track_and_can_be_checked_by_round_trip(signup_user, voices, finish):
    client, ws = await _setup(signup_user)
    media, _ = await _with_transcript(ws, language="en")
    started = await client.post("/api/v1/video-narrate/dub", json={"media_id": media["id"], "language": "es"}, headers=_h(ws))
    assert started.status_code == 202, started.text
    dub_id = started.json()["dub"]["id"]
    assert started.json()["run"]["kind"] == "video"
    deadline = time.monotonic() + 120
    while True:
        got = (await client.get(f"/api/v1/video-narrate/dub/{dub_id}", headers=_h(ws))).json()
        if got["status"] != "running" or time.monotonic() > deadline:
            break
        await asyncio.sleep(1)
    assert got["status"] == "done", got
    assert got["language"] == "es" and got["media_id"] and got["summary"]["lines"] == len(got["lines"]) > 0
    tr = (await client.get(f"/api/v1/video-narrate/tracks/{media['id']}", headers=_h(ws))).json()["tracks"]
    assert [t["id"] for t in tr] == ["original", dub_id] and tr[1]["ai_voice"] is True and tr[1]["media_id"] == got["media_id"]
    listed = (await client.get(f"/api/v1/video-narrate/dub?media_id={media['id']}", headers=_h(ws))).json()["dubs"]
    assert len(listed) == 1

    pending = (await client.get(f"/api/v1/video-narrate/dub/{dub_id}/check", headers=_h(ws))).json()
    assert pending["status"] == "needs_transcript"
    heard = " ".join(ln["text"] for ln in got["lines"])
    tid = str(uuid4())
    await get_db()["video_transcripts"].insert_one({"id": tid, "workspace_id": ws, "media_id": got["media_id"], "status": "done", "language": "es"})
    await get_db()["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0,
                                                         "words": [{"w": w, "s_ms": i * 300, "e_ms": i * 300 + 250} for i, w in enumerate(heard.split())]})
    check = (await client.get(f"/api/v1/video-narrate/dub/{dub_id}/check", headers=_h(ws))).json()
    assert check["status"] == "done" and check["similarity"] == 1.0 and check["verdict"] == "good"


async def test_narration_checks_the_script_the_music_and_the_voice_before_starting(signup_user, voices, finish):
    client, ws = await _setup(signup_user)
    media = await _video(ws, duration=20.0)
    empty = await client.post("/api/v1/video-narrate/narration", json={"media_id": media["id"]}, headers=_h(ws))
    assert empty.status_code == 400
    bad_music = await client.post("/api/v1/video-narrate/narration", json={"media_id": media["id"], "script": "Hello there.",
                                                                             "music_media_id": media["id"]}, headers=_h(ws))
    assert bad_music.status_code == 404
    ok = await client.post("/api/v1/video-narrate/narration", json={"media_id": media["id"], "script": "Hello there, welcome."}, headers=_h(ws))
    assert ok.status_code == 202 and ok.json()["run"]["kind"] == "video"


async def test_captions_are_translated_for_many_videos_in_one_run_and_downloaded_as_a_caption_file(signup_user, voices):
    client, ws = await _setup(signup_user)
    a, _ = await _with_transcript(ws, language="en")
    b, _ = await _with_transcript(ws, language="en")
    nothing = await _video(ws)
    res = await client.post("/api/v1/video-narrate/captions/localize",
                            json={"media_ids": [a["id"], b["id"], nothing["id"], "ghost"], "language": "es"}, headers=_h(ws))
    assert res.status_code == 202, res.text
    assert res.json()["videos"] == 2 and {s["reason"] for s in res.json()["skipped"]} == {"Has no transcript yet.", "Not in this workspace's library."}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        langs = (await client.get(f"/api/v1/video-narrate/captions/{b['id']}", headers=_h(ws))).json()["languages"]
        if langs:
            break
        await asyncio.sleep(1)
    assert langs and langs[0]["language"] == "es"
    srt = await client.get(f"/api/v1/video-narrate/captions/{a['id']}/es.srt", headers=_h(ws))
    assert srt.status_code == 200 and srt.text.startswith("1\n00:00:00,000 --> ") and "Linea traducida" in srt.text
    none = await client.post("/api/v1/video-narrate/captions/localize", json={"media_ids": [nothing["id"]], "language": "es"}, headers=_h(ws))
    assert none.status_code == 409


async def test_another_workspace_cannot_dub_or_read_the_tracks(signup_user, voices):
    client, ws = await _setup(signup_user)
    media, _ = await _with_transcript(ws)
    other, _ = await signup_user()
    other_ws = await create_workspace(other, "Elsewhere")
    assert (await other.post("/api/v1/video-narrate/dub", json={"media_id": media["id"], "language": "es"}, headers=_h(other_ws))).status_code == 404
    assert (await other.get(f"/api/v1/video-narrate/tracks/{media['id']}", headers=_h(other_ws))).status_code == 404


async def test_the_library_says_how_many_videos_can_have_their_captions_translated(signup_user, voices):
    client, ws = await _setup(signup_user)
    assert (await client.get("/api/v1/video-narrate/captions-ready", headers=_h(ws))).json() == {"videos": 0, "total": 0, "limit": 50}
    await _with_transcript(ws, language="en")
    await _with_transcript(ws, language="en")
    await _video(ws)  # a video with no transcript is not counted
    assert (await client.get("/api/v1/video-narrate/captions-ready", headers=_h(ws))).json() == {"videos": 2, "total": 2, "limit": 50}
    other, _ = await signup_user()
    other_ws = await create_workspace(other, "Elsewhere")
    assert (await other.get("/api/v1/video-narrate/captions-ready", headers=_h(other_ws))).json()["videos"] == 0
