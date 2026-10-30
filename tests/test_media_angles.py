"""Fresh Angles for pictures, recordings and video clips: 3 ideas of the same kind, then the ticked ones made through each
pipeline's own function.

Runs with the LLM mocked and the making functions replaced, so no picture, voice or render provider is ever called.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.billing import credits
from app.db.mongo import audio_assets, image_assets
from app.pipelines.text import media_angles as M
from app.pipelines.text.media_angles import MediaAngleIdea
from tests.conftest import create_workspace


def _words(text: str, start: float = 0.0, step: float = 0.5) -> list[dict]:
    out = []
    for i, word in enumerate(text.split()):
        out.append({"word": word, "start_s": round(start + i * step, 2), "end_s": round(start + i * step + 0.4, 2)})
    return out


# ── the ideas the model gives are checked before they are shown ──────────

def test_picture_ideas_need_a_headline_and_scene_and_a_known_look():
    result = {"angles": [
        {"name": "Contrarian", "rationale": "r", "headline": "Most teams post the same clip five times", "scene": "A desk at dawn.", "look": "warm_analog"},
        {"name": "No scene", "headline": "Something", "scene": ""},
        {"name": "Banned", "headline": "Pure synergy", "scene": "A room."},
        {"name": "Odd look", "headline": "One video, five platforms", "scene": "A city at night.", "look": "neon_dream"},
    ]}
    ideas = M.usable_visual_ideas(result, source="one video five posts", banned=["synergy"], fallback_look="midnight_swiss")
    assert [i.name for i in ideas] == ["Contrarian", "Odd look"]
    assert ideas[0].look == "warm_analog" and ideas[1].look == "midnight_swiss"
    assert all(i.action == "picture" and i.units == 1 for i in ideas)


def test_recording_ideas_carry_their_spoken_length():
    script = " ".join(["word"] * 150)
    ideas = M.usable_audio_ideas({"angles": [{"name": "Why", "script": script}, {"name": "Short", "script": "Too short."}]},
                                 source="source", banned=[], max_chars=30000)
    assert [i.name for i in ideas] == ["Why"]
    assert ideas[0].action == "narration" and ideas[0].units == pytest.approx(1.0, abs=0.05)


def test_cuts_land_on_real_words_never_overlap_and_quote_what_was_said():
    words = _words(" ".join(f"w{i}" for i in range(200)))
    result = {"cuts": [
        {"name": "A", "start_s": 0.0, "end_s": 20.4, "hook": "The first take"},
        {"name": "Overlaps A", "start_s": 10.0, "end_s": 30.4, "hook": "Clash"},
        {"name": "B", "start_s": 40.0, "end_s": 60.4, "hook": "The second take"},
        {"name": "No hook", "start_s": 70.0, "end_s": 90.4, "hook": ""},
    ]}
    cuts = M.usable_cuts(result, words, banned=[])
    assert [c.name for c in cuts] == ["A", "B"]
    assert cuts[0].quote.startswith("w0 w1") and cuts[1].start_s == 40.0
    assert cuts[1].action == "render" and cuts[1].units == pytest.approx((cuts[1].end_s - cuts[1].start_s) / 60)


def test_edited_ideas_are_checked_again_before_they_are_made():
    with pytest.raises(HTTPException) as no_scene:
        M.check_ideas("image", [MediaAngleIdea(name="A", headline="Hi", scene=" ")])
    assert no_scene.value.status_code == 422
    with pytest.raises(HTTPException):
        M.check_ideas("audio", [MediaAngleIdea(name="A", script="Hi.")])
    words = _words(" ".join(f"w{i}" for i in range(100)))
    with pytest.raises(HTTPException):
        M.check_ideas("video", [MediaAngleIdea(name="A", start_s=500.0, end_s=520.0, hook="x")], words)
    kept = M.check_ideas("image", [MediaAngleIdea(name="A", headline="Hi there", scene="A desk.", look="not_a_look")])
    assert kept[0].look is None


async def test_the_total_cost_is_each_piece_at_its_own_price():
    ideas = [MediaAngleIdea(name="A", headline="h", scene="s"), MediaAngleIdea(name="B", headline="h", scene="s")]
    assert await M.credits_needed(ideas, "image") == 4
    cut = [MediaAngleIdea(name="A", start_s=0.0, end_s=30.0, hook="h")]
    assert await M.credits_needed(cut, "video") == 3          # half a minute at 5 a minute, rounded up


# ── making: one failing piece never loses the others ─────────────────────

@pytest.fixture
def quiet_run(monkeypatch):
    from app.shared import pipeline_runs

    async def nothing(*args, **kwargs):
        return None

    for name in ("checkpoint", "set_stage", "mark_progress"):
        monkeypatch.setattr(pipeline_runs, name, nothing)


async def test_a_failed_piece_is_noted_and_the_rest_are_still_made(monkeypatch, quiet_run):
    calls = []

    async def make(idea, source, ctx):
        calls.append(idea.name)
        if idea.name == "B":
            raise HTTPException(status_code=503, detail="The picture service did not answer.")
        return {"kind": "image", "id": idea.name, "name": idea.name, "href": f"/x/{idea.name}"}

    monkeypatch.setattr(M, "_make_picture", make)
    ideas = [MediaAngleIdea(name=n, headline="h", scene="s") for n in ("A", "B", "C")]
    result = await M.make_pieces("run1", "image", {"title": "T"}, None, ideas, ctx=None)
    assert calls == ["A", "B", "C"]
    assert [m["id"] for m in result["made"]] == ["A", "C"]
    assert result["failed"] == [{"name": "B", "reason": "The picture service did not answer."}]
    assert result["href"] == "/dashboard/library?type=image"


async def test_running_out_of_credits_stops_the_rest(monkeypatch, quiet_run):
    calls = []

    async def make(idea, source, ctx):
        calls.append(idea.name)
        if idea.name == "B":
            raise credits.CreditsExhausted({"limit": "credits_month", "message": "You have used this month's credits."})
        return {"kind": "image", "id": idea.name, "name": idea.name, "href": "/x"}

    monkeypatch.setattr(M, "_make_picture", make)
    ideas = [MediaAngleIdea(name=n, headline="h", scene="s") for n in ("A", "B", "C")]
    result = await M.make_pieces("run1", "image", {}, None, ideas, ctx=None)
    assert calls == ["A", "B"]
    assert [f["name"] for f in result["failed"]] == ["B", "C"]


async def test_nothing_made_fails_the_run(monkeypatch, quiet_run):
    async def make(idea, source, ctx):
        raise HTTPException(status_code=503, detail="Down.")

    monkeypatch.setattr(M, "_make_picture", make)
    with pytest.raises(HTTPException) as failed:
        await M.make_pieces("run1", "image", {}, None, [MediaAngleIdea(name="A", headline="h", scene="s")], ctx=None)
    assert "None of the angles could be made" in failed.value.detail


# ── the routes ───────────────────────────────────────────────────────────

async def _brand(client, ws_id: str) -> str:
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id})
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def test_picture_ideas_come_back_as_pictures(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Media angles WS")
    brand_id = await _brand(client, ws_id)
    asset_id = str(uuid4())
    await image_assets.insert_one({
        "id": asset_id, "workspace_id": ws_id, "brand_id": brand_id, "title": "One video, five posts", "prompt": "A laptop on a desk",
        "visual_profile": "midnight_swiss", "slides": [{"layout": "linkedin_post", "text_content": {"headline": "Turn one video into five posts"}}],
    })
    mock_llm.set_structured({"angles": [
        {"name": "Contrarian", "rationale": "r", "headline": "Stop posting the same clip", "scene": "A crowded feed.", "look": "cyber_editorial"},
        {"name": "The why", "rationale": "r", "headline": "Every platform reads differently", "scene": "Five screens.", "look": "warm_analog"},
        {"name": "Concrete outcome", "rationale": "r", "headline": "Five posts from one video", "scene": "A tidy desk.", "look": "mono_technical"},
    ]})
    res = await client.post("/api/v1/text/angles/media/ideas", json={"brand_id": brand_id, "asset_kind": "image", "asset_id": asset_id},
                            headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["ideas"]) == 3 and body["title"] == "One video, five posts"
    assert {i["action"] for i in body["ideas"]} == {"picture"} and all(i["scene"] for i in body["ideas"])


async def test_video_ideas_need_the_video(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Media angles WS")
    brand_id = await _brand(client, ws_id)
    res = await client.post("/api/v1/text/angles/media/ideas", json={"brand_id": brand_id, "asset_kind": "video", "asset_id": "a"},
                            headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 400


async def test_video_ideas_are_cuts_of_the_recording(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Media angles WS")
    brand_id = await _brand(client, ws_id)
    audio_id = str(uuid4())
    await audio_assets.insert_one({
        "id": audio_id, "workspace_id": ws_id, "brand_id": brand_id, "title": "Episode 1",
        "transcript": _words(" ".join(f"w{i}" for i in range(300))),
        "video_clips": [{"id": "clip1", "media_id": "m", "start_s": 0.0, "end_s": 20.0, "style": "cover", "size": "vertical",
                         "created_by": "u", "created_at": datetime.now(timezone.utc)}],
    })
    mock_llm.set_structured({"cuts": [
        {"name": "Contrarian", "rationale": "r", "start_s": 30.0, "end_s": 50.4, "hook": "Most people get this wrong"},
        {"name": "The why", "rationale": "r", "start_s": 60.0, "end_s": 80.4, "hook": "Here is why it matters"},
    ]})
    res = await client.post("/api/v1/text/angles/media/ideas",
                            json={"brand_id": brand_id, "asset_kind": "video", "asset_id": audio_id, "clip_id": "clip1"},
                            headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    ideas = res.json()["ideas"]
    assert [i["name"] for i in ideas] == ["Contrarian", "The why"] and all(i["action"] == "render" for i in ideas)


async def test_making_is_refused_up_front_when_the_credits_do_not_fit(signup_user, mock_llm):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Media angles WS")
    brand_id = await _brand(client, ws_id)
    asset_id = str(uuid4())
    await image_assets.insert_one({"id": asset_id, "workspace_id": ws_id, "brand_id": brand_id, "title": "T", "slides": [{}]})
    await credits.set_owner_limit(ws_id, 3)                   # room for one picture, not two
    ideas = [{"name": n, "headline": "h", "scene": "s", "look": "warm_analog"} for n in ("A", "B")]
    res = await client.post("/api/v1/text/angles/media/make",
                            json={"brand_id": brand_id, "asset_kind": "image", "asset_id": asset_id, "ideas": ideas},
                            headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 402, res.text


async def test_a_part_made_run_says_what_was_not_made(monkeypatch, quiet_run):
    async def make(idea, source, ctx):
        if idea.name == "B":
            raise HTTPException(status_code=503, detail="Down.")
        return {"kind": "image", "id": idea.name, "name": idea.name, "href": "/x"}

    monkeypatch.setattr(M, "_make_picture", make)
    ideas = [MediaAngleIdea(name=n, headline="h", scene="s") for n in ("A", "B")]
    result = await M.make_pieces("run1", "image", {}, None, ideas, ctx=None)
    assert result["summary"] == "1 of 2 made. Not made: B: Down."
    full = await M.make_pieces("run1", "image", {}, None, [ideas[0]], ctx=None)
    assert full["summary"] is None


async def test_ideas_are_written_in_the_voice_of_the_items_own_brand(signup_user, mock_llm, monkeypatch):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Media angles WS")
    own_brand = await _brand(client, ws_id)
    other_brand = await _brand(client, ws_id)
    asset_id = str(uuid4())
    await image_assets.insert_one({"id": asset_id, "workspace_id": ws_id, "brand_id": own_brand, "title": "T",
                                   "slides": [{"text_content": {"headline": "Some words"}}]})
    seen = {}

    async def plan(kind, asset_id, *, workspace_id, brand_context, banned, clip_id=None):
        seen["context"] = brand_context
        return [MediaAngleIdea(name="A", headline="h", scene="s", action="picture")], {"title": "T"}

    from app.api.v1 import text as text_routes
    from app.pipelines.text.brand_context import build_brand_context

    monkeypatch.setattr(text_routes, "plan_media_ideas", plan)
    res = await client.post("/api/v1/text/angles/media/ideas", json={"brand_id": other_brand, "asset_kind": "image", "asset_id": asset_id},
                            headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    own = await M.source_brand_id("image", asset_id, ws_id)
    assert own == own_brand
    from app.db.mongo import brand_profiles

    assert seen["context"] == build_brand_context(await brand_profiles.find_one({"id": own_brand}))
