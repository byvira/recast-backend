"""Reading a video: sentences and windows, the cached and paced readings, validators that take every time from the real
transcript, the silence and filler cut list, the shot log and the API. Only the model call itself is replaced (no quota)."""
import asyncio
import time
import re
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db.mongo import get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video.analysis import analyze as A
from app.video.analysis import silence as S
from app.video.analysis import windows as W
from tests.conftest import create_workspace


def _h(ws: str) -> dict:
    return {"X-Workspace-Id": ws}


def _words(n_sentences: int, words_per: int = 8, step_ms: int = 500, gap_ms: int = 300) -> list[dict]:
    out, t = [], 0
    for s in range(n_sentences):
        for k in range(words_per):
            last = k == words_per - 1
            out.append({"w": f"word{s}x{k}" + ("." if last else ""), "s_ms": t, "e_ms": t + step_ms - 100})
            t += step_ms
        t += gap_ms
    return out


# ── sentences and windows ────────────────────────────────────────────────────────────────────────────────────────────────

def test_words_become_sentences_at_marks_at_long_pauses_and_at_a_length_limit():
    words = [{"w": "Hello", "s_ms": 0, "e_ms": 300}, {"w": "there.", "s_ms": 400, "e_ms": 700},
             {"w": "and", "s_ms": 800, "e_ms": 900}, {"w": "then", "s_ms": 1000, "e_ms": 1100},
             {"w": "after", "s_ms": 4000, "e_ms": 4200}, {"w": "a", "s_ms": 4300, "e_ms": 4400}, {"w": "gap", "s_ms": 4500, "e_ms": 4700}]
    sents = W.sentences(words)
    assert [s["text"] for s in sents] == ["Hello there.", "and then", "after a gap"]
    assert sents[1]["start_ms"] == 800 and sents[1]["end_ms"] == 1100
    runaway = [{"w": f"w{i}", "s_ms": i * 100, "e_ms": i * 100 + 50} for i in range(150)]
    assert len(W.sentences(runaway)) == 3                       # a speaker who never stops still gives pieces of 60 words


def test_a_script_with_its_own_full_stop_ends_sentences_too():
    words = [{"w": "नमस्ते।", "s_ms": 0, "e_ms": 400}, {"w": "आज", "s_ms": 500, "e_ms": 800},
             {"w": "बात", "s_ms": 900, "e_ms": 1200}]
    assert [s["first"] for s in W.sentences(words)] == [0, 1]


def test_windows_never_split_a_sentence_and_share_a_few_at_each_edge():
    sents = W.sentences(_words(80))
    wins = W.windows(sents, max_words=160, shared=2)
    assert len(wins) >= 4
    for a, b in zip(wins, wins[1:]):
        assert b["from"] <= a["to"] - 1 and b["from"] > a["from"]          # overlap of shared sentences, always moving forward
    assert wins[0]["from"] == 0 and wins[-1]["to"] == len(sents) - 1
    assert all(sum(len(s["text"].split()) for s in sents[w["from"]:w["to"] + 1]) <= 160 for w in wins)


def test_the_text_a_prompt_reads_is_numbered_and_timed():
    sents = W.sentences(_words(3))
    text = W.window_text(sents, {"from": 0, "to": 2})
    assert text.splitlines()[0].startswith("0 (0:00:00) word0x0 ")
    assert text.splitlines()[2].startswith("2 (")


# ── validating what the model says ───────────────────────────────────────────────────────────────────────────────────────

def test_a_reading_keeps_only_points_that_name_a_real_sentence_in_the_window():
    win = {"from": 10, "to": 20, "index": 0}
    out = A.validate_reading({"summary": "  A — talk about focus.  ", "topics": ["focus", "", "habits"],
                              "points": [{"sentence": 12, "point": "Focus matters."}, {"sentence": 99, "point": "Invented."},
                                         {"sentence": "x", "point": "Bad."}, {"sentence": 15, "point": ""}]}, win)
    assert out["summary"] == "A , talk about focus." and out["topics"] == ["focus", "habits"]
    assert out["points"] == [{"sentence": 12, "point": "Focus matters."}]


def test_chapters_take_their_times_from_the_parts_they_start_in_and_are_kept_apart():
    starts = {1: 0, 2: 120_000, 3: 125_000, 4: 600_000}
    out = A.validate_chapters(
        [{"title": "Intro", "part": 1}, {"title": "Main idea", "part": 2}, {"title": "Too close", "part": 3},
         {"title": "Ghost", "part": 9}, {"title": "Wrap up", "part": 4}], starts, duration_ms=700_000)
    assert [c["title"] for c in out] == ["Intro", "Main idea", "Wrap up"]
    assert out[0]["start_ms"] == 0 and out[2]["start_ms"] == 600_000
    assert A.validate_chapters([{"title": "Only", "part": 1}], starts, 700_000) == []


def _sents(n=40):
    return W.sentences(_words(n))


def test_moments_are_snapped_to_real_sentences_and_checked_for_length_and_range():
    sents = _sents(40)                                         # a sentence is 4 seconds
    win = {"from": 0, "to": 39, "index": 0}
    raw = {"moments": [
        {"start": 2, "end": 8, "title": "A real idea — here", "reason": "It makes one full point.", "hook": 0.9, "standalone": 0.8, "risk": ["needs_context", "made_up"]},
        {"start": 5, "end": 6, "title": "Too short", "reason": "x", "hook": 0.5, "standalone": 0.5},
        {"start": 30, "end": 80, "title": "Out of range", "reason": "x", "hook": 0.5, "standalone": 0.5},
        {"start": 9, "end": 4, "title": "Backwards", "reason": "x", "hook": 0.5, "standalone": 0.5},
        {"start": 10, "end": 15, "title": "", "reason": "No title", "hook": 0.5, "standalone": 0.5},
    ]}
    out = A.validate_moments(raw, win, sents, min_s=20, max_s=60)
    assert len(out) == 1
    m = out[0]
    assert (m["start_ms"], m["end_ms"]) == (sents[2]["start_ms"], sents[8]["end_ms"])      # times come from the real sentences
    assert m["title"] == "A real idea , here" and m["risk"] == ["needs_context"]
    assert m["score"] == pytest.approx(0.6 * 0.9 + 0.4 * 0.8, abs=0.001)


def test_overlapping_moments_keep_the_better_one_and_come_back_in_time_order():
    mk = lambda a, b, score: {"start_ms": a, "end_ms": b, "score": score, "title": "t", "reason": "r"}  # noqa: E731
    picked = A.rank_moments([mk(0, 30_000, 0.5), mk(20_000, 50_000, 0.9), mk(60_000, 90_000, 0.4), mk(95_000, 120_000, 0.7)], count=3)
    assert [(m["start_ms"], m["state"]) for m in picked] == [(20_000, "suggested"), (60_000, "suggested"), (95_000, "suggested")]
    assert [m["id"] for m in picked] == ["m1", "m2", "m3"]


# ── a whole read: caching and pacing ─────────────────────────────────────────────────────────────────────────────────────

class FakeModel:
    """Answers by what the prompt asks for, using the sentence numbers it was given. Counts every call."""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> dict:
        self.calls.append(prompt)
        numbers = [int(n) for n in re.findall(r"^(\d+) \(", prompt, re.M)]
        if "reading one part" in prompt:
            return {"summary": "They talk about focus and habits.", "topics": ["focus", "habits"],
                    "points": [{"sentence": numbers[0], "point": "Focus beats willpower."}]}
        if "joining summaries" in prompt:
            parts = [int(n) for n in re.findall(r"^Part (\d+):", prompt, re.M)]
            return {"title": "Focus and habits", "summary": "A talk about calmer weeks.", "topics": ["focus"],
                    "chapters": [{"title": f"Part {p}", "part": p} for p in parts[::2]]}
        if "finding moments" in prompt:
            a = numbers[0] + 1
            return {"moments": [{"start": a, "end": a + 6, "title": "A clear idea", "reason": "One full point.", "hook": 0.8, "standalone": 0.7, "risk": []}]}
        return {}


@pytest.fixture(autouse=True)
async def _clean_cache(monkeypatch):
    await get_db()["video_analysis_cache"].delete_many({})
    monkeypatch.setattr(W, "WINDOW_WORDS", 200)           # many small windows from a short fixture
    monkeypatch.setattr(A.settings, "GROQ_TPM_LIMIT", 10_000_000)  # the stand-in model has no per-minute ceiling to wait for
    for name in ("video_analysis", "video_transcripts", "video_transcript_pages", "video_transcript_chunks"):
        await get_db()[name].delete_many({})


async def test_a_video_is_read_window_by_window_into_an_outline_chapters_and_moments():
    model = FakeModel()
    result = await A.analyze(_words(120), language="en", duration_s=960.0, caller=model)
    assert result["windows"] >= 4 and result["outline"]["title"] == "Focus and habits"
    assert len(result["chapters"]) >= 2 and result["chapters"][0]["start_ms"] == 0
    assert all(c["start_ms"] < 960_000 for c in result["chapters"])
    assert result["moments"] and all(m["state"] == "suggested" and m["end_ms"] > m["start_ms"] for m in result["moments"])
    assert sum("reading one part" in c for c in model.calls) == result["windows"]


async def test_a_second_read_of_the_same_video_costs_no_model_call():
    model = FakeModel()
    first = await A.analyze(_words(120), duration_s=960.0, caller=model)
    spent = len(model.calls)
    second = await A.analyze(_words(120), duration_s=960.0, caller=model)
    assert len(model.calls) == spent and second["cache_hits"] == second["windows"]
    assert [m["start_ms"] for m in second["moments"]] == [m["start_ms"] for m in first["moments"]]


async def test_a_changed_prompt_or_a_changed_transcript_is_not_served_an_old_answer():
    model = FakeModel()
    await A.analyze(_words(120), duration_s=960.0, caller=model)
    before = len(model.calls)
    other = _words(120)
    other[5]["w"] = "different"
    await A.analyze(other, duration_s=960.0, caller=model)
    assert len(model.calls) > before


async def test_a_video_with_hardly_any_speech_says_so_instead_of_inventing_chapters():
    result = await A.analyze(_words(1, words_per=2), duration_s=5.0, caller=FakeModel())
    assert result["chapters"] == [] and result["moments"] == [] and "too little speech" in result["note"]


async def test_the_pacer_waits_when_the_next_call_would_pass_the_per_minute_ceiling(monkeypatch):
    waited: list[float] = []

    async def fake_sleep(seconds):
        waited.append(seconds)
        # time moves on so the loop can end
        pacer._seen.clear()

    monkeypatch.setattr(A.asyncio, "sleep", fake_sleep)
    pacer = A.Pacer(tokens_per_minute=1000)
    await pacer.wait(600)
    await pacer.wait(600)                # 1200 in one minute: it has to wait
    assert waited and waited[0] >= 0.5


# ── silence, fillers, shots ──────────────────────────────────────────────────────────────────────────────────────────────

def _spoken(text: str, gap_after: dict[int, int] | None = None) -> list[dict]:
    out, t = [], 0
    for i, w in enumerate(text.split()):
        out.append({"w": w, "s_ms": t, "e_ms": t + 250})
        t += 250 + (gap_after or {}).get(i, 60)
    return out


def test_long_pauses_are_found_with_breathing_room_left_on_each_side():
    words = _spoken("so here is the point", {1: 1200})
    cuts = S.pauses(words, pause_ms=600)
    assert len(cuts) == 1
    gap = words[2]["s_ms"] - words[1]["e_ms"]
    assert cuts[0]["start_ms"] == words[1]["e_ms"] + S.KEEP_MS and cuts[0]["end_ms"] == words[2]["s_ms"] - S.KEEP_MS
    assert gap == 1200 and S.pauses(words, pause_ms=1500) == []


async def test_certain_fillers_are_listed_and_unsure_ones_are_left_alone_without_a_judge():
    words = _spoken("um the plan is like very simple you know and uh clear")
    plain = await S.fillers(words, "en")
    assert [f["word"] for f in plain] == ["um", "you know", "uh"]                 # "like" is not touched without a judge


async def test_an_unsure_word_goes_only_when_the_judge_says_it_is_filler():
    words = _spoken("the plan is like very simple and like a map")

    async def judge(items):
        return {it["id"]: it["id"] == 0 for it in items}                           # only the first "like" is filler

    out = await S.fillers(words, "en", judge=judge)
    assert len(out) == 1 and out[0]["word"] == "like" and out[0]["start_ms"] == words[3]["s_ms"]

    async def broken(items):
        raise RuntimeError("busy")

    assert await S.fillers(words, "en", judge=broken) == []                       # an unsure judge removes nothing


def test_the_cut_list_merges_overlaps_and_adds_up_the_time_saved():
    items = [
        {"kind": "pause", "start_ms": 1000, "end_ms": 2000, "reason": "A pause.", "confidence": 0.9},
        {"kind": "filler", "start_ms": 1900, "end_ms": 2300, "reason": "A filler.", "confidence": 0.9},
        {"kind": "pause", "start_ms": 5000, "end_ms": 5800, "reason": "A pause.", "confidence": 0.7},
    ]
    cuts = S.cut_list(items)
    assert [(c["start_ms"], c["end_ms"], c["kind"]) for c in cuts] == [(1000, 2300, "mixed"), (5000, 5800, "pause")]
    assert S.saved_ms(cuts) == 1300 + 800 and [c["id"] for c in cuts] == ["cut1", "cut2"]


def test_silences_are_read_from_the_waveform_peaks():
    peaks = [90] * 20 + [2] * 30 + [80] * 10 + [3] * 5 + [70] * 5          # 20 peaks a second: 1.5 s of silence, then 0.25 s
    out = S.silences_from_peaks(peaks, 20, threshold=6, min_ms=700)
    assert len(out) == 1 and out[0]["start_ms"] == 1000 and out[0]["end_ms"] == 2500


def test_the_shot_log_is_the_stretches_between_cuts():
    log = S.shots([{"t": 12.5, "score": 0.6}, {"t": 30.0, "score": 0.5}, {"t": 30.1, "score": 0.4}, {"t": 999, "score": 0.9}], 60.0)
    assert [(s["start_s"], s["end_s"]) for s in log] == [(0.0, 12.5), (12.5, 30.0), (30.1, 60.0)]
    assert S.shots([], 45.0) == [{"index": 1, "start_s": 0.0, "end_s": 45.0, "duration_s": 45.0}]


# ── the API ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _video_with_transcript(ws: str, words: list[dict], *, preview: dict | None = None) -> str:
    media_id = str(uuid4())
    row = MediaAsset(
        id=media_id, workspace_id=ws, kind=MediaKind.VIDEO, url="https://res.cloudinary.com/x/video/upload/v1/t.mp4", mime_type="video/mp4",
        duration_s=960.0, source=MediaSource.UPLOADED, created_by="u1", created_at=datetime.now(timezone.utc),
    ).model_dump()
    if preview:
        row.update(preview_status="ready", preview=preview)
    await media_assets.insert_one(row)
    tid = str(uuid4())
    await get_db()["video_transcripts"].insert_one({
        "id": tid, "workspace_id": ws, "media_id": media_id, "status": "done", "language": "en", "duration_s": 960.0, "chunks": [],
        "word_count": len(words), "created_at": datetime.now(timezone.utc),
    })
    await get_db()["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0, "words": words})
    return media_id


async def test_reading_needs_a_transcript_first(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Analysis")
    media_id = str(uuid4())
    await media_assets.insert_one(MediaAsset(
        id=media_id, workspace_id=ws, kind=MediaKind.VIDEO, url="https://x/y.mp4", mime_type="video/mp4", source=MediaSource.UPLOADED,
        created_by="u", created_at=datetime.now(timezone.utc), duration_s=10.0).model_dump())
    res = await client.post(f"/api/v1/video-analysis/{media_id}", json={}, headers=_h(ws))
    assert res.status_code == 409 and "transcript" in res.json()["detail"].lower()


async def test_the_api_reads_the_video_lets_moments_be_accepted_and_exports_chapters(signup_user, monkeypatch):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Analysis")
    media_id = await _video_with_transcript(ws, _words(120))
    model = FakeModel()
    monkeypatch.setattr(A, "_default_caller", model)

    started = await client.post(f"/api/v1/video-analysis/{media_id}", json={"parts": ["outline", "moments"], "count": 3}, headers=_h(ws))
    assert started.status_code == 202, started.text
    deadline = time.monotonic() + 240
    while True:
        got = (await client.get(f"/api/v1/video-analysis/{media_id}", headers=_h(ws))).json()["analysis"]
        if got["status"] != "running" or time.monotonic() > deadline:
            break
        await asyncio.sleep(1.0)
    assert got["status"] == "done", got
    assert got["outline"]["title"] == "Focus and habits" and got["moments"]
    first = got["moments"][0]
    acc = await client.post(f"/api/v1/video-analysis/{media_id}/moments/{first['id']}", json={"state": "accepted"}, headers=_h(ws))
    assert acc.status_code == 200 and acc.json()["analysis"]["moments"][0]["state"] == "accepted"
    missing = await client.post(f"/api/v1/video-analysis/{media_id}/moments/m99", json={"state": "rejected"}, headers=_h(ws))
    assert missing.status_code == 404

    txt = await client.get(f"/api/v1/video-analysis/{media_id}/export?kind=chapters&format=txt", headers=_h(ws))
    assert txt.status_code == 200 and txt.text.splitlines()[0].startswith("0:00 ")
    csv_ = await client.get(f"/api/v1/video-analysis/{media_id}/export?kind=moments&format=csv", headers=_h(ws))
    assert csv_.text.splitlines()[0] == "start,end,title or reason,kind"


async def test_the_cut_list_and_the_shot_log_are_available_and_honest_about_what_is_not_ready(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Cuts")
    words = _spoken("um so the plan is clear and then a long pause here we go", {5: 1500})
    media_id = await _video_with_transcript(ws, words)
    cuts = (await client.get(f"/api/v1/video-analysis/{media_id}/cuts?pause_ms=600", headers=_h(ws))).json()
    assert cuts["preview_only"] is True and cuts["count"] >= 2 and cuts["saved_ms"] > 1000
    assert {c["kind"] for c in cuts["cuts"]} >= {"pause", "filler"}
    shots = await client.get(f"/api/v1/video-analysis/{media_id}/shots", headers=_h(ws))
    assert shots.status_code == 409 and "preview" in shots.json()["detail"]

    ready = await _video_with_transcript(ws, words, preview={"meta": {"scenes": [{"t": 3.0, "score": 0.6}], "duration_s": 960.0}})
    log = (await client.get(f"/api/v1/video-analysis/{ready}/shots", headers=_h(ws))).json()
    assert [(s["start_s"], s["end_s"]) for s in log["shots"]] == [(0.0, 3.0), (3.0, 960.0)]
