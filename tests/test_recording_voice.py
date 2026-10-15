"""Remy learning a member's voice from their own recordings: off by default, only the member's own transcripts, bounded work, one
run an hour, reversible, and the render cost in Ops that stays honest while the host is free."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.agents.personal import recording_voice as rv
from app.core.config import settings
from app.db.mongo import audio_assets, get_db, media_assets


def _words(n: int, prefix: str = "w") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


def test_passages_are_spread_through_the_whole_recording_and_short_ones_give_none():
    assert rv.passages(_words(30)) == []
    spread = rv.passages(_words(1000))
    assert len(spread) == rv.PASSAGES_PER_SOURCE
    assert spread[0].startswith("w0 ") and spread[1].startswith("w333 ") and spread[2].startswith("w666 ")
    assert all(len(p.split()) == rv.PASSAGE_WORDS for p in spread)


async def _embed(text: str) -> list[float]:
    _embed.calls += 1
    return [1.0, 0.0, 0.0] if "yes" in text else [0.0, 1.0, 0.0]


_embed.calls = 0


async def _own_video(ws: str, user: str, words: list[str], *, creator: str | None = None) -> str:
    mid = uuid4().hex
    await media_assets.insert_one({"id": mid, "workspace_id": ws, "kind": "video", "created_by": creator or user, "created_at": datetime.now(timezone.utc),
                                   "url": "https://x/v.mp4"})
    tid = uuid4().hex
    await get_db()["video_transcripts"].insert_one({"id": tid, "workspace_id": ws, "media_id": mid, "status": "done"})
    await get_db()["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0, "words": [{"w": w, "s_ms": i * 100, "e_ms": i * 100 + 90} for i, w in enumerate(words)]})
    return mid


async def test_nothing_is_learned_until_the_member_switches_it_on_and_forgetting_removes_it_all():
    ws, user = f"ws-{uuid4()}", f"u-{uuid4()}"
    await _own_video(ws, user, _words(600, "yes"))

    assert (await rv.status(ws, user))["enabled"] is False
    assert (await rv.learn(ws, user, embed=_embed))["result"] == "off"

    await rv.set_enabled(ws, user, True)
    learned = await rv.learn(ws, user, embed=_embed)
    assert learned["result"] == "learned" and learned["ready"] is True and learned["sources"] == 1 and learned["passages"] >= rv.MIN_PASSAGES_TO_USE

    gone = await rv.forget(ws, user)
    assert gone["enabled"] is False and gone["passages"] == 0 and gone["sources"] == 0
    assert await get_db()["remy_recording_voice"].count_documents({"workspace_id": ws}) == 0


async def test_only_the_members_own_recordings_are_used_and_each_is_used_once():
    ws, mine, other = f"ws-{uuid4()}", f"u-{uuid4()}", f"u-{uuid4()}"
    await _own_video(ws, mine, _words(600, "yes"))
    await _own_video(ws, other, _words(600, "yes"), creator=other)
    await audio_assets.insert_one({"id": uuid4().hex, "workspace_id": ws, "created_by": other, "created_at": datetime.now(timezone.utc),
                                   "transcript": [{"word": w, "start_s": i, "end_s": i + 1} for i, w in enumerate(_words(400, "yes"))]})
    await rv.set_enabled(ws, mine, True)

    first = await rv.learn(ws, mine, embed=_embed)
    assert first["sources"] == 1                                                     # the other member's two sources were not read

    later = datetime.now(timezone.utc) + timedelta(hours=2)
    again = await rv.learn(ws, mine, embed=_embed, now=later)
    assert again["result"] == "nothing_new" and again["sources"] == 1                # the same source is not learned twice


async def test_learning_is_bounded_and_runs_at_most_once_an_hour():
    ws, user = f"ws-{uuid4()}", f"u-{uuid4()}"
    for _ in range(6):
        await _own_video(ws, user, _words(900, "yes"))
    await rv.set_enabled(ws, user, True)
    _embed.calls = 0

    first = await rv.learn(ws, user, embed=_embed)

    assert first["result"] == "learned" and _embed.calls <= rv.MAX_PASSAGES and first["passages"] <= rv.MAX_PASSAGES
    assert (await rv.learn(ws, user, embed=_embed))["result"] == "too_soon"


async def test_a_text_close_to_the_learned_voice_passes_and_a_distant_one_drifts_and_off_says_so():
    ws, user = f"ws-{uuid4()}", f"u-{uuid4()}"
    assert (await rv.compare(ws, user, "anything", embed=_embed))["status"] == "off"
    await _own_video(ws, user, _words(600, "yes"))
    await rv.set_enabled(ws, user, True)
    assert (await rv.compare(ws, user, "anything", embed=_embed))["status"] == "learning"
    await rv.learn(ws, user, embed=_embed)

    near = await rv.compare(ws, user, "yes yes yes", embed=_embed)
    far = await rv.compare(ws, user, "something else", embed=_embed)

    assert near["status"] == "ok" and near["in_voice"] is True
    assert far["status"] == "ok" and far["in_voice"] is False


async def test_a_failed_embedding_learns_nothing_instead_of_storing_an_empty_voice():
    ws, user = f"ws-{uuid4()}", f"u-{uuid4()}"
    await _own_video(ws, user, _words(600, "yes"))
    await rv.set_enabled(ws, user, True)

    async def broken(text):
        return []

    out = await rv.learn(ws, user, embed=broken)

    assert out["result"] == "unavailable" and out["ready"] is False


async def test_ops_shows_an_estimated_cost_only_when_a_rate_is_set(signup_user, monkeypatch):
    from app.core import auth

    ws = f"ws-{uuid4()}"
    await get_db()["video_jobs"].insert_one({"id": uuid4().hex, "workspace_id": ws, "status": "done", "duration_s": 600, "created_at": datetime.now(timezone.utc)})
    from app.api.v1 import ops_video

    async def staff():
        return {"id": "staff"}

    from app.main import app
    app.dependency_overrides[auth.require_platform_staff] = staff
    try:
        client, _ = await signup_user()
        free = (await client.get("/api/v1/ops/video/usage")).json()
        mine = next(w for w in free["workspaces"] if w["workspace_id"] == ws)
        assert free["cost_per_minute_usd"] == 0 and mine["estimated_cost_usd"] == 0

        monkeypatch.setattr(settings, "VIDEO_RENDER_COST_PER_MINUTE", 0.05)
        paid = (await client.get("/api/v1/ops/video/usage")).json()
        mine = next(w for w in paid["workspaces"] if w["workspace_id"] == ws)
        assert mine["estimated_cost_usd"] == 0.5 and paid["estimated_cost_usd"] >= 0.5
    finally:
        app.dependency_overrides.pop(auth.require_platform_staff, None)
