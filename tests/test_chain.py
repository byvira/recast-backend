"""The topic-to-video chain: one goal across text, audio, image and video, with one decision log and a lineage on everything it makes.

The database, the plan executor, the edit list, the project store and the lineage are real. Only the toolkit's four provider-facing
tools (write the post, voice it, make the picture, render) are replaced, as the other agent tests do."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.chain import lineage, topic_to_video as chain
from app.agents.runtime import toolkit
from app.agents.runtime.tools import ToolResult
from app.db.mongo import get_db

COLLECTIONS = ("content_pieces", "audio_assets", "media_assets", "video_projects", "video_project_versions", "chain_runs", "agent_decisions")


class _Reporter:
    def __init__(self):
        self.labels: list[str] = []

    async def step(self, label: str) -> None:
        self.labels.append(label)


class _World:
    def __init__(self):
        self.ws = f"ws-{uuid4()}"
        self.piece, self.audio, self.voice_media, self.cover, self.video = (str(uuid4()) for _ in range(5))
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        self.ctx = SimpleNamespace(workspace_id=self.ws, user_id="u1", member=None)

    async def seed(self):
        db = get_db()
        await db["content_pieces"].insert_one({"id": self.piece, "workspace_id": self.ws, "content": "A post."})
        await db["audio_assets"].insert_one({"id": self.audio, "workspace_id": self.ws})
        await db["media_assets"].insert_many([
            {"id": self.voice_media, "workspace_id": self.ws, "kind": "audio", "url": "https://cdn.example/voice.mp3", "duration_s": 6.0,
             "source": "synthesized"},
            {"id": self.cover, "workspace_id": self.ws, "kind": "image", "url": "https://cdn.example/cover.png", "source": "ai_generated"},
            {"id": self.video, "workspace_id": self.ws, "kind": "video", "url": "https://cdn.example/out.mp4", "source": "rendered"},
        ])

    async def use(self, name, *, ctx, reporter=None, **kwargs):
        self.calls.append((name, kwargs))
        if name in self.fail:
            return ToolResult(name, False, error=f"{name} could not finish.")
        if name == "text.write":
            return ToolResult(name, True, SimpleNamespace(pieces=[SimpleNamespace(piece_id=self.piece, content="Plan the week on Sunday and protect two hours.")]))
        if name == "audio.narrate":
            return ToolResult(name, True, {"asset_id": self.audio, "media_id": self.voice_media, "origin": "ai"})
        if name == "image.generate":
            return ToolResult(name, True, {"media_id": self.cover, "origin": "ai", "qa_flagged": False})
        if name == "video.render":
            return ToolResult(name, True, {"media_id": self.video})
        raise AssertionError(f"unexpected tool {name}")

    async def cleanup(self):
        for c in COLLECTIONS:
            await get_db()[c].delete_many({"workspace_id": self.ws})


@pytest.fixture
async def world(monkeypatch):
    w = _World()
    await w.seed()
    monkeypatch.setattr(toolkit, "use", w.use)
    yield w
    await w.cleanup()


def _payload(**settings):
    base = {"topic": "How to plan a week", "brand_id": "brand-1"}
    return SimpleNamespace(settings=chain.ChainSettings(**{**base, **settings}))


async def test_a_topic_becomes_a_rendered_video_and_every_step_is_logged(world):
    reporter = _Reporter()
    out = await chain.run_chain(world.ctx, _payload(), reporter)
    assert out["ok"] and [s["status"] for s in out["steps"]] == ["done"] * 5
    assert [name for name, _ in world.calls] == ["text.write", "audio.narrate", "image.generate", "video.render"]
    assert reporter.labels == ["Writing the post", "Voicing the post", "Making the cover picture", "Building the video", "Rendering the video"]
    # the post goes into the voice step by its id, so the narration is of the post that was written
    assert world.calls[1][1]["request"]["source_piece_id"] == world.piece
    project = await get_db()["video_projects"].find_one({"id": out["project_id"], "workspace_id": world.ws})
    edit = project["edit"]
    assert edit["video"][0]["media_id"] == world.cover and edit["audio"][0]["media_id"] == world.voice_media
    assert edit["captions"]["enabled"] and edit["duration_f"] == 180            # six seconds at thirty frames
    decisions = await get_db()["agent_decisions"].count_documents({"workspace_id": world.ws, "agent": "chain"})
    assert decisions == 5
    assert await get_db()["chain_runs"].count_documents({"workspace_id": world.ws, "id": out["chain_id"], "ok": True}) == 1


async def test_everything_the_chain_made_says_what_it_came_from(world):
    out = await chain.run_chain(world.ctx, _payload(), _Reporter())
    db = get_db()
    audio = await db["audio_assets"].find_one({"id": world.audio})
    assert audio["derived_from"] == [{"kind": "piece", "id": world.piece}]
    cover = await db["media_assets"].find_one({"id": world.cover})
    assert cover["derived_from"] == [{"kind": "piece", "id": world.piece}]
    back = await lineage.ancestors("media", world.video, world.ws)
    found = {(a["kind"], a["id"]) for a in back}
    assert found == {("project", out["project_id"]), ("audio", world.audio), ("image", world.cover), ("piece", world.piece)}


async def test_a_failed_cover_leaves_the_brand_colour_and_the_chain_carries_on(world):
    world.fail.add("image.generate")
    out = await chain.run_chain(world.ctx, _payload(), _Reporter())
    assert out["ok"] is True and out["partial"] is True and out["video_media_id"] == world.video
    statuses = {s["tool"]: s["status"] for s in out["steps"]}
    assert statuses["cover"] == "failed" and statuses["assemble"] == "done" and statuses["render"] == "done"
    project = await get_db()["video_projects"].find_one({"id": out["project_id"], "workspace_id": world.ws})
    assert project["edit"]["video"] == []


async def test_a_failed_narration_stops_the_chain_and_asks_for_a_decision(world):
    world.fail.add("audio.narrate")
    out = await chain.run_chain(world.ctx, _payload(), _Reporter())
    statuses = {s["tool"]: s["status"] for s in out["steps"]}
    assert statuses["write"] == "done" and statuses["narrate"] == "failed"
    assert statuses["assemble"] == "not_run" and statuses["render"] == "not_run"
    assert out["ok"] is False and out["project_id"] is None
    needs = await get_db()["agent_decisions"].find_one({"workspace_id": world.ws, "agent": "chain", "step": "chain"})
    assert needs and needs["outcome"] == "needs_decision"


async def test_without_a_render_the_project_is_left_to_open(world):
    out = await chain.run_chain(world.ctx, _payload(render=False, cover=False), _Reporter())
    assert out["ok"] and out["project_id"] and out["video_media_id"] is None
    assert [name for name, _ in world.calls] == ["text.write", "audio.narrate"]


def test_the_plan_is_the_same_every_time():
    a = [s.tool for s in chain.build_plan(chain.ChainSettings(topic="A topic", brand_id="b"))]
    b = [s.tool for s in chain.build_plan(chain.ChainSettings(topic="A topic", brand_id="b"))]
    assert a == b == ["write", "narrate", "cover", "assemble", "render"]


async def test_a_lineage_cycle_ends_quietly(world):
    db = get_db()
    await db["audio_assets"].update_one({"id": world.audio}, {"$set": {"derived_from": [{"kind": "piece", "id": world.piece}]}})
    await db["content_pieces"].update_one({"id": world.piece}, {"$set": {"derived_from": [{"kind": "audio", "id": world.audio}]}})
    back = await lineage.ancestors("audio", world.audio, world.ws)
    assert {(a["kind"], a["id"]) for a in back} == {("piece", world.piece), ("audio", world.audio)}
