"""The cross-type toolkit: permission, kill switch and cost class on every tool; narration, pictures, voice check and video
render through it; and Odette's new read tools over the shared decision log.

Provider-facing calls (speech, the picture service, storage, the language model) are replaced as in the other tests. The worker
boundary for video is exercised by writing the job's end state to the real database, the way the worker's report does."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.runtime import record_decision
from app.agents.runtime.toolkit import TOOLKIT, use
from app.agents.supervisor.tools import make_tools
from app.db.mongo import get_db, media_assets, workspaces
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.shared import jobs as jobs_layer
from app.video import edl, jobs as video_jobs, service
from tests.test_audio_assets import _setup, stubs  # noqa: F401 - fixture reuse


def _ctx(ws: str, role: str = "owner", user: str = "u1"):
    return SimpleNamespace(workspace_id=ws, user_id=user, member={"role": role, "user_id": user, "status": "active"})


# ── the registry ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_content_type_is_in_the_toolkit_with_a_cost_class():
    names = set(TOOLKIT.names())
    assert {"text.write", "image.generate", "audio.narrate", "audio.transcribe", "video.render", "remy.check_voice"} <= names
    assert TOOLKIT.spec("video.render").cost == "worker"
    assert TOOLKIT.spec("image.generate").cost == "provider"
    assert TOOLKIT.spec("audio.narrate").cost == "provider"
    assert TOOLKIT.spec("text.write").cost == "llm"
    assert TOOLKIT.spec("analytics.top_assets").cost == "free"


async def test_a_viewer_cannot_use_a_generating_tool_and_gets_a_plain_refusal():
    res = await use("text.write", ctx=_ctx(f"ws-{uuid4()}", role="viewer"), payload={"topic": "x"})
    assert res.ok is False and "permission" in (res.error or "").lower()


async def test_the_kill_switch_stops_every_generating_tool(signup_user, stubs):  # noqa: F811
    client, profile, ws_id, brand_id = await _setup(signup_user)
    await workspaces.update_one({"id": ws_id}, {"$set": {"generation_halted": True}})
    ctx = await jobs_layer.build_context(ws_id, profile["id"])
    for tool, kwargs in (
        ("audio.narrate", {"request": {"title": "t", "brand_id": brand_id, "script": "Hello there."}}),
        ("image.generate", {"topic": "morning", "brand_id": brand_id}),
        ("text.write", {"payload": {"topic": "x"}}),
    ):
        res = await use(tool, ctx=ctx, **kwargs)
        assert res.ok is False and "paused" in (res.error or "").lower(), tool
    assert stubs["synth"] == []                 # nothing was voiced


# ── a chain across types ─────────────────────────────────────────────────────────────────────────────────────────────────

async def test_narration_through_the_toolkit_makes_a_real_recording_flagged_ai(signup_user, stubs):  # noqa: F811
    client, profile, ws_id, brand_id = await _setup(signup_user)
    ctx = await jobs_layer.build_context(ws_id, profile["id"])
    res = await use("audio.narrate", ctx=ctx, request={"title": "Episode", "brand_id": brand_id, "script": "Welcome to the show."})
    assert res.ok, res.error
    assert res.value["origin"] == "ai" and res.value["asset_id"]
    asset = await get_db()["audio_assets"].find_one({"id": res.value["asset_id"]})
    media = await media_assets.find_one({"id": asset["media_id"]})
    assert media["origin"] == "ai" and media["contains_ai"] is True       # the narration is marked AI generated everywhere
    assert stubs["synth"][0]["text"] == "Welcome to the show."


async def test_the_voice_check_refuses_empty_text_plainly(signup_user):
    client, profile = await signup_user()
    from tests.conftest import create_workspace

    ws = await create_workspace(client, "Voice check")
    ctx = await jobs_layer.build_context(ws, profile["id"])
    res = await use("remy.check_voice", ctx=ctx, text="   ")
    assert res.ok is False and "no text" in res.error.lower()


async def test_a_toolkit_step_that_is_not_valid_is_refused_before_it_runs(signup_user):
    client, profile = await signup_user()
    from tests.conftest import create_workspace

    ws = await create_workspace(client, "Bad input")
    ctx = await jobs_layer.build_context(ws, profile["id"])
    res = await use("audio.dialogue", ctx=ctx, payload={"not": "a valid request"})
    assert res.ok is False and "not valid" in res.error


# ── video render through the toolkit ─────────────────────────────────────────────────────────────────────────────────────

async def _project(ws: str, user: str) -> str:
    mid = str(uuid4())
    await media_assets.insert_one(MediaAsset(
        id=mid, workspace_id=ws, kind=MediaKind.VIDEO, url="https://res.cloudinary.com/x/video/upload/v1/a.mp4", mime_type="video/mp4",
        duration_s=10.0, source=MediaSource.UPLOADED, created_by=user, created_at=datetime.now(timezone.utc),
    ).model_dump())
    edit = edl.validate({**edl.blank("landscape"), "video": [{"id": "v1", "media_id": mid, "at_f": 0, "dur_f": 300}]})
    pid = str(uuid4())
    await get_db()["video_projects"].insert_one({
        "id": pid, "workspace_id": ws, "title": "Chain test", "edit": edit, "initial_edit": edit, "version": 1, "status": "draft",
        "created_by": user, "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    })
    return pid


async def test_render_and_wait_returns_the_finished_video_when_the_worker_reports(signup_user):
    client, profile = await signup_user()
    from tests.conftest import create_workspace

    ws = await create_workspace(client, "Render wait")
    pid = await _project(ws, profile["id"])
    steps: list[str] = []

    class Rep:
        async def step(self, label):
            steps.append(label)

    async def worker_finishes():
        for _ in range(60):
            job = await get_db()["video_jobs"].find_one({"workspace_id": ws, "project_id": pid})
            if job:
                await get_db()["video_jobs"].update_one({"id": job["id"]}, {"$set": {"progress": {"stage": "Making your video (1 of 1)"}}})
                await asyncio.sleep(service.POLL_SECONDS + 0.5)
                await get_db()["video_jobs"].update_one(
                    {"id": job["id"]}, {"$set": {"status": "done", "media_id": "finished-media", "finished_at": datetime.now(timezone.utc)}})
                return
            await asyncio.sleep(0.2)

    done, _ = await asyncio.gather(
        service.render_project_and_wait(workspace_id=ws, user_id=profile["id"], project_id=pid, reporter=Rep(), max_wait_s=60),
        worker_finishes(),
    )
    assert done["media_id"] == "finished-media" and done["job_id"]
    assert "Making your video (1 of 1)" in steps


async def test_a_render_that_the_worker_fails_is_reported_in_plain_words(signup_user):
    client, profile = await signup_user()
    from tests.conftest import create_workspace

    ws = await create_workspace(client, "Render fail")
    pid = await _project(ws, profile["id"])

    async def worker_fails():
        for _ in range(60):
            job = await get_db()["video_jobs"].find_one({"workspace_id": ws, "project_id": pid})
            if job:
                await get_db()["video_jobs"].update_one({"id": job["id"]}, {"$set": {"status": "failed", "error": "This video could not be read."}})
                return
            await asyncio.sleep(0.2)

    with pytest.raises(service.RenderFailed) as caught:
        await asyncio.gather(
            service.render_project_and_wait(workspace_id=ws, user_id=profile["id"], project_id=pid, max_wait_s=60), worker_fails(),
        )
    assert "could not be read" in str(caught.value)


async def test_a_render_that_takes_too_long_says_so_and_stays_queued(signup_user):
    client, profile = await signup_user()
    from tests.conftest import create_workspace

    ws = await create_workspace(client, "Render slow")
    pid = await _project(ws, profile["id"])
    with pytest.raises(service.RenderFailed) as caught:
        await service.render_project_and_wait(workspace_id=ws, user_id=profile["id"], project_id=pid, max_wait_s=1)
    assert "longer than expected" in str(caught.value)
    job = await get_db()["video_jobs"].find_one({"workspace_id": ws, "project_id": pid})
    assert job["status"] == "queued"             # the job is still there and will be picked up by a worker


# ── Odette reads the shared decision log ─────────────────────────────────────────────────────────────────────────────────

async def test_odette_can_read_what_the_agents_did_and_what_waits_for_a_person():
    ws = f"ws-{uuid4()}"
    await record_decision(workspace_id=ws, agent="audio", step="chapters", summary="Finding chapters: done", run_id="r1")
    await record_decision(workspace_id=ws, agent="image", step="generate_picture", summary="Made a picture.")
    await record_decision(workspace_id=ws, agent="audio", step="signoff", summary="Ready for your sign-off.",
                          outcome="needs_decision", evidence={"audio_asset_id": "a-1"})
    await record_decision(workspace_id=f"ws-{uuid4()}", agent="audio", step="chapters", summary="Another workspace")
    _, dispatch = make_tools(ws)

    everything = await dispatch("get_agent_decisions", {})
    assert everything["count"] == 3 and {d["agent"] for d in everything["decisions"]} == {"audio", "image"}
    only_audio = await dispatch("get_agent_decisions", {"agent": "audio"})
    assert only_audio["count"] == 2
    waiting = await dispatch("get_needs_decision", {})
    assert waiting["count"] == 1 and waiting["items"][0]["subject"] == "a-1"


async def test_odette_reads_which_assets_performed_best():
    ws = f"ws-{uuid4()}"
    piece = str(uuid4())
    await get_db()["post_metrics"].insert_one({
        "workspace_id": ws, "platform": "linkedin", "post_id": piece, "platform_post_id": f"p-{piece}", "likes": 12,
        "impressions": 200, "engagement_rate": 6.0, "fetched_at": datetime.now(timezone.utc),
    })
    await get_db()["image_assets"].insert_one({
        "id": "img-1", "workspace_id": ws, "title": "Sunrise", "deleted": False, "linked_pieces": [{"piece_id": piece}],
    })
    _, dispatch = make_tools(ws)
    out = await dispatch("get_asset_performance", {"asset_type": "image"})
    assert out["items"][0]["asset_id"] == "img-1" and out["items"][0]["average_engagement_rate"] == 6.0
    assert (await dispatch("get_asset_performance", {"asset_type": "video"}))["error"]
