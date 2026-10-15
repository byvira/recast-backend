"""The video director: a goal becomes a validated plan over the flows' own builders and the render service. The database, the plan
executor, the project store and the decision log are real; the flow's builder (so no provider is called), the render and Remy's
check are replaced."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.runtime import toolkit
from app.agents.runtime.tools import ToolResult
from app.agents.video import director
from app.db.mongo import get_db
from app.video import edl, service
from app.video.flows import registry as R


class _Reporter:
    def __init__(self):
        self.labels = []

    async def step(self, label):
        self.labels.append(label)


@pytest.fixture
async def ws(monkeypatch):
    ws_id = f"ws-{uuid4()}"
    ctx = SimpleNamespace(workspace_id=ws_id, user_id="u1", member=None)
    calls = {"render": [], "voice": []}

    async def builder(env, params):
        edit = edl.blank("vertical")
        return [{"title": f"Clip {i}", "edit": edit, "created_from": {"kind": "flow", "flow": "post_to_video"}, "notes": []} for i in (1, 2, 3, 4)]

    async def render(**kwargs):
        calls["render"].append(kwargs["project_id"])
        return {"media_id": f"m-{len(calls['render'])}"}

    async def use(name, *, ctx, reporter=None, **kwargs):
        calls["voice"].append(kwargs["text"])
        return ToolResult(name, True, {"aligned": True})

    monkeypatch.setitem(R.FLOWS["post_to_video"], "fn", builder)
    monkeypatch.setattr(service, "render_project_and_wait", render)
    monkeypatch.setattr(toolkit, "use", use)
    yield ctx, calls
    for c in ("video_projects", "video_project_versions", "agent_decisions"):
        await get_db()[c].delete_many({"workspace_id": ws_id})


def _payload(**kw):
    base = {"flow": "post_to_video", "params": {"text": "A post about planning a week well."}}
    return SimpleNamespace(settings=director.DirectorSettings(**{**base, **kw}))


async def test_a_post_becomes_projects_checked_and_rendered_up_to_the_limit(ws):
    ctx, calls = ws
    reporter = _Reporter()
    out = await director.run_director(ctx, _payload(max_renders=2), reporter)
    assert out["ok"] and len(out["projects"]) == 4
    assert reporter.labels == ["Checking the source", "Building the projects", "Checking the titles against your voice", "Rendering"]
    assert len(calls["render"]) == 2                                        # the limit; the other two stay as projects
    assert sum(1 for p in out["projects"] if p.get("media_id")) == 2
    assert calls["voice"] == ["Clip 1", "Clip 2", "Clip 3", "Clip 4"]
    assert out["needs_review"] is True
    entry = await get_db()["agent_decisions"].find_one({"workspace_id": ctx.workspace_id, "agent": "video", "step": "review"})
    assert entry and entry["outcome"] == "needs_decision"


async def test_a_flow_that_cannot_start_says_why_and_makes_nothing(ws):
    ctx, calls = ws
    out = await director.run_director(ctx, _payload(params={}), _Reporter())
    assert out["ok"] is False and "needs: text" in out["error"]
    assert out["projects"] == [] and calls["render"] == []
    assert await get_db()["video_projects"].count_documents({"workspace_id": ctx.workspace_id}) == 0


async def test_without_a_render_the_projects_are_left_to_open(ws):
    ctx, calls = ws
    out = await director.run_director(ctx, _payload(render=False, voice_check=False), _Reporter())
    assert out["ok"] and calls["render"] == [] and calls["voice"] == []
    assert [s["tool"] for s in out["steps"]] == ["check_source", "build"]
    assert await get_db()["video_projects"].count_documents({"workspace_id": ctx.workspace_id}) == 4


async def test_a_render_that_fails_keeps_the_projects_and_says_so(ws, monkeypatch):
    ctx, calls = ws

    async def broken(**kwargs):
        raise service.RenderFailed("The render host is busy.")

    monkeypatch.setattr(service, "render_project_and_wait", broken)
    out = await director.run_director(ctx, _payload(), _Reporter())
    assert out["ok"] is True                                                 # the projects exist; only the render is reported
    statuses = {s["tool"]: s["status"] for s in out["steps"]}
    assert statuses["build"] == "done" and statuses["render"] == "failed"
    assert all("error" in p for p in out["projects"][:3])


def test_the_plan_is_the_same_every_time_and_the_limit_is_bounded():
    a = [s.tool for s in director.build_plan(director.DirectorSettings(flow="post_to_video", params={"text": "x" * 30}))]
    assert a == ["check_source", "build", "voice_check", "render"]
    with pytest.raises(Exception):
        director.DirectorSettings(flow="post_to_video", max_renders=50)
