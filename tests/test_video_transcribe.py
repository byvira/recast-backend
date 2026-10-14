"""Transcription of video of any length: the plan, the overlap merge, the shared speech allowance, resume and retry, and the API.

The sound is cut by real ffmpeg from a real file. Only the call to the speech service itself is replaced (no provider quota)."""
import asyncio
import io
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import numpy as np
import pytest
import soundfile as sf

from app.core.config import settings
from app.db.mongo import get_db, media_assets
from app.models.audio_asset import TranscriptWord
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import transcribe
from tests.conftest import create_workspace


def _h(ws: str) -> dict:
    return {"X-Workspace-Id": ws}


# ── planning ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_three_hour_file_is_nine_pieces_that_overlap_and_cover_everything():
    plan = transcribe.plan_chunks(10800.0)
    assert len(plan) == 9
    assert plan[0]["start_s"] == 0.0 and plan[1]["start_s"] == pytest.approx(1198.0)       # starts two seconds early
    assert plan[-1]["owns_to_s"] == 10800.0
    for a, b in zip(plan, plan[1:]):
        assert a["owns_to_s"] == b["owns_from_s"]                                          # no gap and no double ownership


def test_a_short_remainder_is_folded_into_the_last_piece():
    plan = transcribe.plan_chunks(1215.0)
    assert len(plan) == 1 and plan[0]["length_s"] == 1215.0
    assert len(transcribe.plan_chunks(1260.0)) == 2


def test_a_file_with_no_length_has_no_pieces():
    assert transcribe.plan_chunks(0) == [] and transcribe.plan_chunks(-5) == []


# ── merging at the joins ─────────────────────────────────────────────────────────────────────────────────────────────────

def _truth(seconds: float, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    out, t = [], 0.05
    while t < seconds - 0.4:
        dur = rng.uniform(0.15, 0.45)
        out.append({"w": f"w{len(out)}", "s_ms": int(t * 1000), "e_ms": int((t + dur) * 1000)})
        t += dur + rng.uniform(0.02, 0.2)
    return out


def _as_pieces(plan: list[dict], truth: list[dict]) -> dict[int, list[dict]]:
    """What the speech service would give for each piece: the words that start inside it, in the piece's own time."""
    pieces = {}
    for ch in plan:
        lo, hi = ch["start_s"] * 1000, (ch["start_s"] + ch["length_s"]) * 1000
        pieces[ch["index"]] = [{**w, "s_ms": int(w["s_ms"] - lo), "e_ms": int(w["e_ms"] - lo)} for w in truth if lo <= w["s_ms"] < hi]
    return pieces


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_merge_loses_no_word_and_repeats_none_at_any_join(seed):
    truth = _truth(3000.0, seed)
    plan = transcribe.plan_chunks(3000.0, chunk_s=1000.0)
    merged = transcribe.merge_words(plan, _as_pieces(plan, truth))
    assert [w["w"] for w in merged] == [w["w"] for w in truth]
    assert all(abs(m["s_ms"] - t["s_ms"]) <= 1 for m, t in zip(merged, truth))


def test_a_missing_piece_leaves_a_hole_not_a_shifted_transcript():
    truth = _truth(3000.0)
    plan = transcribe.plan_chunks(3000.0, chunk_s=1000.0)
    pieces = _as_pieces(plan, truth)
    del pieces[1]
    merged = transcribe.merge_words(plan, pieces)
    # the missing piece owns from the middle of its first overlap (999 s) to the middle of the next (1,999 s)
    assert merged and all(w["s_ms"] < 999_000 or w["s_ms"] >= 1_999_000 for w in merged)
    assert len(merged) < len(truth)


# ── the speech allowance ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def small_allowance(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_TRANSCRIBE_HOURLY_SECONDS", 100)
    monkeypatch.setattr(settings, "VIDEO_TRANSCRIBE_DAILY_SECONDS", 1000)
    await get_db()["video_transcribe_budget"].delete_many({})


async def test_the_allowance_is_counted_and_a_full_hour_says_how_long_to_wait(small_allowance):
    ok, wait = await transcribe.reserve_seconds(60)
    assert ok and wait == 0
    ok, wait = await transcribe.reserve_seconds(60)
    assert ok is False and 0 < wait <= 3700
    ok, _ = await transcribe.reserve_seconds(40)
    assert ok is True                                          # what is left in the hour still fits
    left = await transcribe.budget_left()
    assert left["hour_left_s"] == pytest.approx(0, abs=0.01) and left["day_left_s"] == pytest.approx(900, abs=0.01)


async def test_pieces_asking_together_never_use_more_than_the_allowance(small_allowance):
    results = await asyncio.gather(*[transcribe.reserve_seconds(30) for _ in range(10)])
    assert sum(1 for ok, _ in results if ok) == 3             # 100 seconds an hour fits three pieces of 30


# ── a real run ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def _clean():
    for name in ("video_transcripts", "video_transcript_chunks", "video_transcript_pages", "video_transcribe_budget"):
        await get_db()[name].delete_many({})
    yield


def _tone_file(tmp_path, seconds: float = 70.0):
    t = np.arange(int(16000 * seconds)) / 16000
    buf = io.BytesIO()
    sf.write(buf, (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32"), 16000, format="WAV")
    path = tmp_path / "talk.wav"
    path.write_bytes(buf.getvalue())
    return path


async def _library_item(ws: str, path, seconds: float) -> str:
    row = MediaAsset(
        id=str(uuid4()), workspace_id=ws, kind=MediaKind.VIDEO, url=str(path), mime_type="video/mp4", duration_s=seconds,
        source=MediaSource.UPLOADED, created_by="u1", created_at=datetime.now(timezone.utc),
    )
    await media_assets.insert_one(row.model_dump())
    return row.id


@pytest.fixture
def speech(monkeypatch):
    """The speech service: three words per piece at 1, 2 and 3 seconds into the piece. Records how it was called."""
    calls = {"n": 0, "languages": [], "turbo": [], "fail_next": 0, "detect": "ta"}

    async def fake(audio_bytes, filename, language=None, punctuate=None, prefer_turbo=False):
        calls["n"] += 1
        calls["languages"].append(language)
        calls["turbo"].append(prefer_turbo)
        assert len(audio_bytes) > 1000                           # a real cut of real sound reached it
        if calls["fail_next"] > 0:
            calls["fail_next"] -= 1
            return [], None
        idx = int(filename.replace("piece", "").split(".")[0])
        return [TranscriptWord(word=f"p{idx}w{k}", start_s=float(k), end_s=k + 0.4) for k in (1, 2, 3)], language or calls["detect"]

    monkeypatch.setattr("app.pipelines.audio.transcriber.transcribe_audio_detailed", fake)
    return calls


async def _make(tmp_path, seconds=70.0, chunk=30.0):
    ws = f"ws-{uuid4()}"
    media_id = await _library_item(ws, _tone_file(tmp_path, seconds), seconds)
    doc = await transcribe.create(workspace_id=ws, user_id="u1", media_id=media_id, duration_s=seconds)
    chunks = transcribe.plan_chunks(seconds, chunk_s=chunk)
    await get_db()["video_transcripts"].update_one({"id": doc["id"]}, {"$set": {"chunks": chunks}})
    return ws, media_id, doc["id"]


async def test_a_long_file_is_cut_transcribed_in_pieces_and_joined_in_time_order(tmp_path, speech):
    _, _, tid = await _make(tmp_path, seconds=70.0, chunk=30.0)
    state = await transcribe.run(tid)
    assert state["status"] == "done" and state["chunks_total"] == 2 and state["language"] == "ta" and state["has_speech"] is True
    words = await transcribe.all_words(tid)
    assert [w["w"] for w in words] == ["p0w1", "p0w2", "p0w3", "p1w1", "p1w2", "p1w3"]
    assert words[3]["s_ms"] == pytest.approx(30_000 - 2_000 + 1_000, abs=2)       # the second piece starts two seconds early
    assert speech["languages"] == [None, "ta"]                                    # the first piece sets the language for the rest


async def test_english_uses_the_fast_model_first_and_other_languages_the_large_one(tmp_path, speech):
    ws = f"ws-{uuid4()}"
    media_id = await _library_item(ws, _tone_file(tmp_path, 20.0), 20.0)
    doc = await transcribe.create(workspace_id=ws, user_id="u1", media_id=media_id, duration_s=20.0, language="en")
    await transcribe.run(doc["id"])
    assert speech["turbo"] == [True]
    speech["turbo"].clear()
    other = await transcribe.create(workspace_id=ws, user_id="u1", media_id=await _library_item(ws, _tone_file(tmp_path, 20.0), 20.0),
                                    duration_s=20.0, language="hi")
    await transcribe.run(other["id"])
    assert speech["turbo"] == [False]


async def test_a_piece_that_fails_is_tried_again_on_its_own_and_the_others_are_not_repeated(tmp_path, speech):
    _, _, tid = await _make(tmp_path, seconds=70.0, chunk=30.0)
    speech["fail_next"] = 1                                   # the first piece fails once
    first = await transcribe.run(tid)
    assert first["status"] == "waiting" and first["chunks_done"] == 1
    calls_before = speech["n"]
    await get_db()["video_transcripts"].update_one({"id": tid}, {"$set": {"waiting_until": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    second = await transcribe.run(tid)
    assert second["status"] == "done" and speech["n"] - calls_before == 1       # only the failed piece went again
    assert [w["w"] for w in await transcribe.all_words(tid)][:3] == ["p0w1", "p0w2", "p0w3"]


async def test_a_piece_that_keeps_failing_ends_as_partial_with_a_plain_message_and_can_be_retried(tmp_path, speech):
    _, _, tid = await _make(tmp_path, seconds=70.0, chunk=30.0)
    speech["fail_next"] = 99
    for _ in range(transcribe.MAX_CHUNK_ATTEMPTS + 1):
        await get_db()["video_transcripts"].update_one({"id": tid}, {"$set": {"lease_until": None}})
        state = await transcribe.run(tid)
    assert state["status"] == "partial" and "could not be transcribed" in state["error"] and state["chunks_failed"] == 2
    speech["fail_next"] = 0
    await transcribe.retry_failed(tid)
    again = await transcribe.run(tid)
    assert again["status"] == "done" and again["error"] is None


async def test_when_the_allowance_runs_out_the_rest_waits_and_carries_on_by_itself(tmp_path, speech, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_TRANSCRIBE_HOURLY_SECONDS", 35)       # room for one 30 second piece an hour
    _, media_id, tid = await _make(tmp_path, seconds=70.0, chunk=30.0)
    first = await transcribe.run(tid)
    assert first["status"] == "waiting" and first["chunks_done"] == 1 and first["waiting_until"]
    # the next hour: the counter is fresh and the waiting time has passed
    await get_db()["video_transcribe_budget"].delete_many({})
    await get_db()["video_transcripts"].update_one({"id": tid}, {"$set": {"waiting_until": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    resumed = await transcribe.continue_waiting()
    assert resumed == 1
    done = await get_db()["video_transcripts"].find_one({"id": tid})
    assert done["status"] in ("waiting", "done")                 # the second piece fits or waits for the next window


async def test_two_runs_never_work_on_the_same_transcript_at_once(tmp_path, speech):
    _, _, tid = await _make(tmp_path, seconds=70.0, chunk=30.0)
    a, b = await asyncio.gather(transcribe.run(tid), transcribe.run(tid))
    assert speech["n"] == 2                                      # each piece was sent once, not twice
    assert "done" in (a.get("status"), b.get("status"))


# ── searching ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_search_finds_a_word_or_phrase_with_the_words_around_it():
    words = [{"w": w, "s_ms": i * 500, "e_ms": i * 500 + 300} for i, w in enumerate("We talked about focus, and focus habits today.".split())]
    hits = transcribe.search(words, "focus")
    assert [h["at_ms"] for h in hits] == [1500, 2500]
    assert transcribe.search(words, "Focus habits")[0]["at_ms"] == 2500
    assert transcribe.search(words, "") == [] and transcribe.search(words, "zebra") == []


# ── the API ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_the_api_starts_reports_pages_searches_and_gives_caption_files(signup_user, tmp_path, speech, monkeypatch):
    monkeypatch.setattr(transcribe, "CHUNK_SECONDS", 30.0)
    client, _ = await signup_user()
    ws = await create_workspace(client, "Transcripts")
    media_id = await _library_item(ws, _tone_file(tmp_path, 70.0), 70.0)

    est = (await client.get(f"/api/v1/video-transcripts/{media_id}/estimate", headers=_h(ws))).json()
    assert est["pieces"] == 2 and est["finishes_now"] is True and est["speech_seconds_to_read"] > 60

    early = await client.get(f"/api/v1/video-transcripts/{media_id}/words", headers=_h(ws))
    assert early.status_code == 409

    started = await client.post(f"/api/v1/video-transcripts/{media_id}", json={}, headers=_h(ws))
    assert started.status_code == 202, started.text
    assert started.headers["cache-control"] == "no-store"
    for _ in range(100):
        state = (await client.get(f"/api/v1/video-transcripts/{media_id}", headers=_h(ws))).json()["transcript"]
        if state and state["status"] == "done":
            break
        await asyncio.sleep(0.3)
    assert state["status"] == "done" and state["progress"] == 1.0

    page = (await client.get(f"/api/v1/video-transcripts/{media_id}/words?page=0", headers=_h(ws))).json()
    assert page["pages"] == 1 and len(page["words"]) == 6 and page["language"] == "ta"
    hits = (await client.get(f"/api/v1/video-transcripts/{media_id}/search?q=p1w2", headers=_h(ws))).json()["hits"]
    assert len(hits) == 1
    srt = await client.get(f"/api/v1/video-transcripts/{media_id}/captions?format=srt", headers=_h(ws))
    assert srt.status_code == 200 and "-->" in srt.text and "p0w1" in srt.text
    vtt = await client.get(f"/api/v1/video-transcripts/{media_id}/captions?format=vtt", headers=_h(ws))
    assert vtt.text.startswith("WEBVTT")


async def test_another_workspaces_video_cannot_be_transcribed_or_read(signup_user, tmp_path):
    client, _ = await signup_user()
    mine = await create_workspace(client, "Mine")
    theirs = f"ws-{uuid4()}"
    media_id = await _library_item(theirs, _tone_file(tmp_path, 10.0), 10.0)
    for method, path in (("get", f"/{media_id}"), ("post", f"/{media_id}"), ("get", f"/{media_id}/estimate"), ("get", f"/{media_id}/words")):
        res = await getattr(client, method)(f"/api/v1/video-transcripts{path}", headers=_h(mine))
        assert res.status_code == 404, (method, path)
