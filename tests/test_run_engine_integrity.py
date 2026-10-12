"""The run engine under races and failures: one active run per start key, the workspace limit holding when starts arrive together,
a finished run never overwritten, a time limit on every run, and a clean shutdown. Talks to the test database directly; no model
or provider is called."""
import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db.mongo import pipeline_runs
from app.shared import pipeline_runs as runs


@pytest.fixture
async def ws():
    await pipeline_runs.create_index("active_key", unique=True, partialFilterExpression={"active_key": {"$type": "string"}})
    workspace_id = f"ws-{uuid4()}"
    yield workspace_id
    await pipeline_runs.delete_many({"workspace_id": workspace_id})


async def _run(ws, **kw):
    return await runs.create_run(workspace_id=ws, user_id="u1", kind=kw.pop("kind", "text"), title="t", **kw)


async def test_only_one_active_run_can_hold_a_start_key(ws):
    key = f"k-{uuid4()}"
    first = await _run(ws, active_key=key)
    with pytest.raises(runs.RunAlreadyActive) as err:
        await _run(ws, active_key=key)
    assert err.value.existing["id"] == first["id"]
    await runs._finish(first, "done")
    again = await _run(ws, active_key=key)  # released when the first one ended
    assert again["id"] != first["id"]


async def test_starts_arriving_together_cannot_pass_the_start_key(ws):
    key = f"k-{uuid4()}"
    results = await asyncio.gather(*[_run(ws, active_key=key) for _ in range(5)], return_exceptions=True)
    made = [r for r in results if isinstance(r, dict)]
    assert len(made) == 1
    assert all(isinstance(r, runs.RunAlreadyActive) for r in results if not isinstance(r, dict))


async def test_the_workspace_limit_holds_when_starts_arrive_together(ws):
    results = await asyncio.gather(*[_run(ws) for _ in range(6)], return_exceptions=True)
    made = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(made) <= runs.MAX_ACTIVE_PER_WORKSPACE
    assert len(made) + len(refused) == 6
    assert all(r.status_code == 429 for r in refused)
    assert await pipeline_runs.count_documents({"workspace_id": ws, "status": {"$in": list(runs.ACTIVE)}}) == len(made)


async def test_a_fourth_run_is_refused_and_not_left_behind(ws):
    for _ in range(runs.MAX_ACTIVE_PER_WORKSPACE):
        await _run(ws)
    with pytest.raises(HTTPException) as err:
        await _run(ws)
    assert err.value.status_code == 429
    assert await pipeline_runs.count_documents({"workspace_id": ws}) == runs.MAX_ACTIVE_PER_WORKSPACE


async def test_a_late_finish_never_overwrites_an_outcome_already_recorded(ws):
    run = await _run(ws)
    await runs._finish(run, "failed", error="interrupted")
    await runs._finish(run, "done", result={"x": 1})
    saved = await pipeline_runs.find_one({"id": run["id"]})
    assert saved["status"] == "failed" and saved["error"] == "interrupted"


async def test_every_kind_of_run_has_a_time_limit(ws, monkeypatch):
    monkeypatch.setattr(runs, "MAX_SECONDS_DEFAULT", 0.3)
    monkeypatch.setattr(runs, "POLL_SECONDS", 0.05)
    run = await _run(ws)

    async def hangs(doc):
        await asyncio.sleep(30)

    runs.start(run, hangs)  # not an interruptible run
    for _ in range(80):
        saved = await pipeline_runs.find_one({"id": run["id"]})
        if saved["status"] in runs.FINISHED:
            break
        await asyncio.sleep(0.1)
    assert saved["status"] == "failed" and "took longer" in saved["error"]
    for _ in range(40):
        if run["id"] not in runs._tasks:
            break
        await asyncio.sleep(0.05)
    assert run["id"] not in runs._tasks


async def test_a_run_that_is_going_here_is_never_swept_as_dead(ws):
    run = await _run(ws)
    old = runs._now() - runs.STALE_AFTER * 3
    await pipeline_runs.update_one({"id": run["id"]}, {"$set": {"heartbeat_at": old, "created_at": old}})
    release = asyncio.Event()

    async def work(doc):
        await release.wait()

    runs.start(run, work)
    await asyncio.sleep(0.1)
    await runs.fail_interrupted()
    assert (await pipeline_runs.find_one({"id": run["id"]}))["status"] != "failed"
    release.set()
    for _ in range(40):
        if (await pipeline_runs.find_one({"id": run["id"]}))["status"] == "done":
            break
        await asyncio.sleep(0.1)
    assert (await pipeline_runs.find_one({"id": run["id"]}))["status"] == "done"


async def test_shutdown_marks_running_work_so_the_next_start_closes_it_at_once(ws):
    run = await _run(ws)

    async def work(doc):
        await asyncio.sleep(30)

    runs.start(run, work)
    await asyncio.sleep(0.2)
    assert await runs.mark_interrupted_for_shutdown() >= 1
    await asyncio.sleep(0.2)
    runs._tasks.pop(run["id"], None)  # the new server has no task for it
    await runs.fail_interrupted()
    saved = await pipeline_runs.find_one({"id": run["id"]})
    assert saved["status"] == "failed" and "interrupted" in saved["error"]
    assert "active_key" not in saved


async def test_pause_on_a_run_nobody_is_working_on_says_so(ws):
    run = await _run(ws)
    old = runs._now() - runs.ORPHAN_AFTER * 2
    await pipeline_runs.update_one({"id": run["id"]}, {"$set": {"created_at": old, "heartbeat_at": old}})
    with pytest.raises(HTTPException) as err:
        await runs.control(run["id"], ws, "pause")
    assert err.value.status_code == 409


async def test_resume_starts_the_reminders_over(ws):
    run = await _run(ws)
    await pipeline_runs.update_one({"id": run["id"]}, {"$set": {"reminders_sent": 2, "pause_requested": True}})
    await runs.control(run["id"], ws, "resume")
    assert (await pipeline_runs.find_one({"id": run["id"]}))["reminders_sent"] == 0
