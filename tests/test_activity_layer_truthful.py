"""What the activity layer shows must be true: runs carry the thing they belong to, say what they are doing, and report real
counts. No model or provider is called."""
import asyncio
from uuid import uuid4

import pytest

from app.db.mongo import pipeline_runs
from app.shared import jobs, pipeline_runs as runs


@pytest.fixture
async def ws():
    await pipeline_runs.create_index("active_key", unique=True, partialFilterExpression={"active_key": {"$type": "string"}})
    workspace_id = f"ws-{uuid4()}"
    yield workspace_id
    await pipeline_runs.delete_many({"workspace_id": workspace_id})


def test_a_run_has_a_percentage_under_its_own_name():
    shown = runs.public({"id": "x", "status": "running", "steps_total": 4, "steps_done": 1})
    assert shown["progress"] == 25 and shown["progress_percent"] == 25
    assert runs.public({"id": "x", "status": "running"})["progress_percent"] is None
    assert runs.public({"id": "x", "status": "done"})["progress_percent"] == 100


def test_odette_is_a_known_run_kind():
    assert "odette" in runs.KINDS


async def test_work_announces_a_stage_only_inside_a_run(ws):
    run = await runs.create_run(workspace_id=ws, user_id="u1", kind="text", title="t")
    await runs.announce_stage("nobody is listening")  # no run bound: nothing happens, nothing fails
    runs.bind_stage_sink(run["id"])
    try:
        await runs.announce_stage("Writing your posts")
    finally:
        runs.bind_stage_sink(None)
    saved = await pipeline_runs.find_one({"id": run["id"]})
    assert saved["stage"] == "Writing your posts"


async def test_the_next_batch_job_shares_the_batch_routes_start_key(ws):
    from app.shared.job_actions import register_all

    if not jobs.ACTIONS:
        register_all()
    action = jobs.ACTIONS["campaign.next_batch"]
    payload = action.payload_model(campaign_id="c-123")
    assert action.ref(payload) == {"campaign_id": "c-123"}
    assert action.active_key(payload) == "campaign:c-123"
    # Other campaign actions (one post's media) are not tied to the whole campaign's batch.
    assert jobs.ACTIONS["campaign.retry_media"].ref is None
    assert jobs.ACTIONS["campaign.retry_media"].active_key is None


async def test_a_second_batch_for_the_same_campaign_cannot_start(ws):
    first = await runs.create_run(
        workspace_id=ws, user_id="u1", kind="campaign", title="b", ref={"campaign_id": "c-9"}, active_key="campaign:c-9",
    )
    with pytest.raises(runs.RunAlreadyActive):
        await runs.create_run(workspace_id=ws, user_id="u1", kind="campaign", title="b2", ref={"campaign_id": "c-9"}, active_key="campaign:c-9")
    found = await runs.active_run_for(ws, "campaign", {"campaign_id": "c-9"})
    assert found and found["id"] == first["id"]


async def test_a_second_odette_pass_is_answered_with_the_running_one(ws, monkeypatch):
    from app.agents.supervisor import service, ticks

    gate = asyncio.Event()

    async def slow(_ctx, _workspace_id):
        await gate.wait()
        return {}

    monkeypatch.setattr(ticks, "run_supervisor_now", slow)
    first = await service.trigger_run(ws, "u1")
    assert first["status"] == "started_inline" and first["run"]["kind"] == "odette"
    second = await service.trigger_run(ws, "u1")
    assert second["status"] == "already_running"
    assert second["run"]["id"] == first["run"]["id"]
    gate.set()
    await asyncio.sleep(0.2)
