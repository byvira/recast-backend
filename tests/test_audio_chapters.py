"""Chapters for a recording. The validator tests are pure (no database or network); the
route tests run with the LLM stubbed and never call a real provider."""

import pytest

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import audio_assets
from app.pipelines.media import chapters as c
from app.prompts.registry import load_prompt
from tests.conftest import create_workspace


def item(start, title="A topic"):
    return {"start_s": start, "title": title}


# ── validator ────────────────────────────────────────────────────────────────

def test_a_normal_set_is_kept_sorted_and_starts_at_zero():
    out = c.normalize_chapters([item(120, "Second"), item(3, "First"), item(300, "Third")], 600)
    assert [x["title"] for x in out] == ["First", "Second", "Third"]
    assert out[0]["start_s"] == 0.0
    assert out[1]["start_s"] == 120


def test_made_up_times_are_dropped():
    out = c.normalize_chapters([item(0, "Start"), item(9999, "Past the end"), item(-5, "Before"), item("x", "Not a number"), item(None)], 600)
    assert [x["title"] for x in out] == ["Start"]


def test_entries_without_a_title_are_dropped():
    assert c.normalize_chapters([item(0, ""), item(100, "   "), {"start_s": 200}, "junk", None], 600) == []


def test_chapters_too_close_together_are_merged():
    out = c.normalize_chapters([item(0, "A"), item(5, "B"), item(14, "C"), item(40, "D")], 600)
    assert [x["title"] for x in out] == ["A", "D"]


def test_no_more_than_the_maximum():
    many = [item(i * 30, f"T{i}") for i in range(30)]
    assert len(c.normalize_chapters(many, 3000)) == c.MAX_CHAPTERS


def test_a_short_recording_is_not_split():
    assert c.normalize_chapters([item(0, "A"), item(30, "B")], 45) == []


def test_titles_are_cleaned_and_prompt_echoes_are_refused():
    assert c.clean_title('  "Why systems win"  ') == "Why systems win"
    assert c.clean_title("Return ONLY the chapters") == ""
    assert c.clean_title('{"chapters": []}') == ""
    assert c.clean_title("Reading the transcript") == "Reading the transcript"
    assert len(c.clean_title("x" * 300)) <= c.MAX_TITLE_CHARS
    assert c.clean_title(None) == ""


def test_transcript_is_cut_into_timed_lines():
    words = [{"word": f"w{i}", "start_s": float(i)} for i in range(25)]
    lines = c.transcript_for_prompt(words, every_seconds=10)
    assert lines[0]["start_s"] == 0.0
    assert len(lines) == 3
    assert c.transcript_for_prompt([]) == []
    assert c.transcript_for_prompt([{"word": "a"}]) == []


def test_the_chapter_playing_at_a_time():
    chapters = [{"title": "A", "start_s": 0}, {"title": "B", "start_s": 60}]
    assert c.chapter_at(chapters, 30)["title"] == "A"
    assert c.chapter_at(chapters, 60)["title"] == "B"
    assert c.chapter_at([], 10) is None


def test_the_prompt_renders_with_times_and_language():
    lines = [{"start_s": 0.0, "text": "hello there"}, {"start_s": 62.0, "text": "next topic"}]
    prompt = load_prompt("audio/chapters", lines=lines, min_chapters=2, max_chapters=5, language_name="Hindi")
    assert "[0s] hello there" in prompt and "[62s] next topic" in prompt
    assert "Hindi" in prompt and "Between 2 and 5 chapters" in prompt


# ── route ────────────────────────────────────────────────────────────────────

async def _asset(ws_id, words, **extra):
    doc = {"id": "aud-ch", "workspace_id": ws_id, "brand_id": "b", "title": "Ep", "transcript": words, **extra}
    await audio_assets.delete_many({"id": "aud-ch"})
    await audio_assets.insert_one(doc)
    return "aud-ch"


def _long_words():
    return [{"word": f"w{i}", "start_s": float(i * 2), "end_s": float(i * 2 + 1.5)} for i in range(150)]  # ~300 s


@pytest.fixture
def llm(monkeypatch):
    seen = {"calls": 0, "reply": {"chapters": []}}

    async def fake(prompt, *a, **k):
        seen["calls"] += 1
        seen["prompt"] = prompt
        return seen["reply"]

    monkeypatch.setattr(audio_module, "call_llm_structured", fake)
    return seen


async def test_chapters_are_generated_checked_and_saved(signup_user, llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Chapters WS")
    aid = await _asset(ws_id, _long_words())
    llm["reply"] = {"chapters": [item(10, "Opening"), item(100, "The middle"), item(9999, "Invented"), item(200, "Wrap up")]}

    res = await client.post(f"/api/v1/audio-assets/{aid}/chapters", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    titles = [x["title"] for x in res.json()["chapters"]]
    assert titles == ["Opening", "The middle", "Wrap up"]
    assert res.json()["chapters"][0]["start_s"] == 0.0
    saved = await audio_assets.find_one({"id": aid})
    assert [x["title"] for x in saved["chapters"]] == titles


async def test_no_transcript_is_a_clear_error_and_no_ai_call(signup_user, llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Chapters WS2")
    aid = await _asset(ws_id, [])
    res = await client.post(f"/api/v1/audio-assets/{aid}/chapters", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 400
    assert llm["calls"] == 0


async def test_a_short_recording_is_not_sent_to_the_ai(signup_user, llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Chapters WS3")
    aid = await _asset(ws_id, [{"word": "hi", "start_s": 0.0, "end_s": 20.0}])
    res = await client.post(f"/api/v1/audio-assets/{aid}/chapters", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200
    assert res.json()["chapters"] == [] and "minute" in res.json()["reason"]
    assert llm["calls"] == 0


async def test_an_unusable_answer_saves_nothing(signup_user, llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Chapters WS4")
    aid = await _asset(ws_id, _long_words(), chapters=[{"title": "Old", "start_s": 0.0}])
    llm["reply"] = {"chapters": [item(9999, "Invented")]}
    res = await client.post(f"/api/v1/audio-assets/{aid}/chapters", headers={"X-Workspace-Id": ws_id})
    assert res.json()["chapters"] == []
    saved = await audio_assets.find_one({"id": aid})
    assert saved["chapters"][0]["title"] == "Old", "a failed attempt must not wipe the existing chapters"


async def test_another_workspaces_recording_is_not_found(signup_user, llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Chapters WS5")
    await audio_assets.delete_many({"id": "aud-other"})
    await audio_assets.insert_one({"id": "aud-other", "workspace_id": "elsewhere", "brand_id": "b", "title": "x", "transcript": _long_words()})
    res = await client.post("/api/v1/audio-assets/aud-other/chapters", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 404
    assert llm["calls"] == 0
