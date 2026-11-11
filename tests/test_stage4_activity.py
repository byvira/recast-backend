"""Stage 4: a failed job that is safe to repeat can be tried again once, cancelling heavy work really stops it, and a workspace is told once
when nothing scheduled is left, with a preview of the next batch. No model is called and nothing real is charged."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.db.mongo import content_pieces, get_campaigns_collection, get_db, workspaces
from app.shared import heavy_work, jobs
from app.shared.heavy_work import run_heavy, stop_point
from app.workers import queue_empty
from tests.conftest import create_workspace, signup_new_user
from tests.test_campaigns import _create_brand, _valid_body
from tests.test_pipeline_runs import _wait_for


class Note(BaseModel):
    note: str = ""


@pytest.fixture
def test_actions():
    created = []

    def add(name, run, **kw):
        jobs.register(jobs.JobAction(name=name, permission="edit_content", payload_model=Note, run=run, title=lambda p: f"Test {name}", kind="text", **kw))
        created.append(name)

    yield add
    for name in created:
        jobs.ACTIONS.pop(name, None)


async def _ws(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Stage 4 WS")
    return client, profile, ws_id, {"X-Workspace-Id": ws_id}


# ---- try again --------------------------------------------------------------------------------------------------------------------------
async def test_a_failed_repeatable_job_can_be_tried_again_once_and_is_offered_in_control_tower(signup_user, test_actions):
    calls = {"n": 0}

    async def flaky(ctx, payload, reporter):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the first try failed")
        return {"ok": True}

    test_actions("test.stage4.flaky", flaky, restartable=True, retries=0)
    client, _, _, headers = await _ws(signup_user)
    first = (await client.post("/api/v1/jobs", json={"action": "test.stage4.flaky", "payload": {"note": "a"}}, headers=headers)).json()
    failed = await _wait_for(client, first["id"], ("failed", "done"), headers=headers)
    assert failed["status"] == "failed" and failed["can_retry"] is True
    assert "payload" not in failed and "retry_run_id" not in failed, "internal fields stay internal"

    tower = (await client.get("/api/v1/activity/control-tower", headers=headers)).json()
    assert next(f for f in tower["failed"] if f["id"] == first["id"])["retry"] == {"kind": "job", "run_id": first["id"]}

    again = await client.post(f"/api/v1/runs/{first['id']}/retry", headers=headers)
    assert again.status_code == 202, again.text
    new = again.json()
    assert new["id"] != first["id"] and new["title"] == failed["title"]
    done = await _wait_for(client, new["id"], ("done", "failed"), headers=headers)
    assert done["status"] == "done" and calls["n"] == 2

    old = (await client.get(f"/api/v1/runs/{first['id']}", headers=headers)).json()
    assert old["can_retry"] is False, "a failure is tried again once"
    second = await client.post(f"/api/v1/runs/{first['id']}/retry", headers=headers)
    assert second.status_code == 409 and "already tried again" in second.json()["detail"]
    tower = (await client.get("/api/v1/activity/control-tower", headers=headers)).json()
    assert not any(f["id"] == first["id"] and f.get("retry") for f in tower["failed"]), "no button once it was tried again"


async def test_two_quick_taps_on_try_again_start_one_new_run(signup_user, test_actions):
    started = {"n": 0}

    async def slow_ok(ctx, payload, reporter):
        started["n"] += 1
        if started["n"] == 1:
            raise RuntimeError("fail first")
        await asyncio.sleep(0.2)
        return {"ok": True}

    test_actions("test.stage4.taps", slow_ok, restartable=True, retries=0)
    client, _, _, headers = await _ws(signup_user)
    first = (await client.post("/api/v1/jobs", json={"action": "test.stage4.taps", "payload": {"note": "t"}}, headers=headers)).json()
    await _wait_for(client, first["id"], ("failed",), headers=headers)
    answers = await asyncio.gather(*[client.post(f"/api/v1/runs/{first['id']}/retry", headers=headers) for _ in range(4)])
    assert sorted(a.status_code for a in answers) == [202, 409, 409, 409], [a.status_code for a in answers]
    await _wait_for(client, next(a for a in answers if a.status_code == 202).json()["id"], ("done",), headers=headers)


async def test_work_that_is_not_safe_to_repeat_is_never_offered_or_repeated(signup_user, test_actions):
    async def always_fails(ctx, payload, reporter):
        raise RuntimeError("no")

    test_actions("test.stage4.once", always_fails, restartable=False, retries=0)
    client, _, _, headers = await _ws(signup_user)
    run = (await client.post("/api/v1/jobs", json={"action": "test.stage4.once", "payload": {"note": "x"}}, headers=headers)).json()
    failed = await _wait_for(client, run["id"], ("failed",), headers=headers)
    assert failed["can_retry"] is False
    refused = await client.post(f"/api/v1/runs/{run['id']}/retry", headers=headers)
    assert refused.status_code == 409 and "can't be repeated" in refused.json()["detail"]
    tower = (await client.get("/api/v1/activity/control-tower", headers=headers)).json()
    assert not next(f for f in tower["failed"] if f["id"] == run["id"]).get("retry")


async def test_only_failed_work_in_your_own_workspace_can_be_tried_again(signup_user, test_actions):
    async def fine(ctx, payload, reporter):
        return {"ok": True}

    test_actions("test.stage4.fine", fine, restartable=True, retries=0)
    client, _, _, headers = await _ws(signup_user)
    run = (await client.post("/api/v1/jobs", json={"action": "test.stage4.fine", "payload": {"note": "ok"}}, headers=headers)).json()
    await _wait_for(client, run["id"], ("done",), headers=headers)
    done = await client.post(f"/api/v1/runs/{run['id']}/retry", headers=headers)
    assert done.status_code == 409 and "failed" in done.json()["detail"]
    assert (await client.post("/api/v1/runs/nope/retry", headers=headers)).status_code == 404
    other, _, _, other_headers = await _ws(signup_user)
    assert (await other.post(f"/api/v1/runs/{run['id']}/retry", headers=other_headers)).status_code == 404


# ---- stopping heavy work ----------------------------------------------------------------------------------------------------------------
async def test_cancelling_heavy_work_stops_it_between_steps_and_leaves_nothing_running():
    steps = []

    def work():
        for i in range(400):
            stop_point()
            steps.append(i)
            time.sleep(0.01)
        return "finished"

    task = asyncio.create_task(run_heavy(work))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stopped_at = len(steps)
    assert 0 < stopped_at < 400, "it ended part way"
    await asyncio.sleep(0.15)
    assert len(steps) == stopped_at, "nothing was left running in the background"


async def test_the_slot_is_held_until_cancelled_work_has_really_ended(monkeypatch):
    monkeypatch.setattr(heavy_work, "_gate", asyncio.Semaphore(1))
    log = {}

    def stubborn():
        time.sleep(0.4)  # no stop points: it can only finish its step
        log["a_end"] = time.monotonic()

    def next_job():
        log["b_start"] = time.monotonic()
        return "b"

    first = asyncio.create_task(run_heavy(stubborn))
    await asyncio.sleep(0.05)
    first.cancel()
    second = asyncio.create_task(run_heavy(next_job))
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == "b"
    assert log["b_start"] >= log["a_end"], "the next heavy job waited for the cancelled one to end"


async def test_heavy_work_that_is_not_cancelled_is_unchanged(monkeypatch):
    monkeypatch.setattr(heavy_work, "_gate", asyncio.Semaphore(1))
    assert await run_heavy(lambda a, b=2: a + b, 3, b=4) == 7
    with pytest.raises(ValueError, match="bad"):
        await run_heavy(lambda: (_ for _ in ()).throw(ValueError("bad")))
    stop_point()  # outside heavy work it does nothing


async def test_a_zip_build_stops_when_cancelled(monkeypatch):
    from app.pipelines.media.zip_export import build_slides_zip

    seen = []

    class Counting(list):
        def __iter__(self):
            for item in super().__iter__():
                seen.append(1)
                time.sleep(0.002)
                yield item

    files = Counting((f"s{i}.png", b"x" * 100) for i in range(2000))
    task = asyncio.create_task(run_heavy(build_slides_zip, files))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 0 < len(seen) < 2000


# ---- the queue-empty notice ------------------------------------------------------------------------------------------------------------
NOW = datetime.now(timezone.utc)


class Told:
    def __init__(self):
        self.rows = []

    async def __call__(self, **kwargs):
        self.rows.append(kwargs)


@pytest.fixture
def told(monkeypatch):
    sink = Told()
    monkeypatch.setattr(queue_empty, "record_system", sink)
    monkeypatch.setattr(queue_empty, "_spare_credits", AsyncMock(return_value=True))
    return sink


async def _workspace(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Queue WS")
    return client, profile, ws_id


async def _post(ws_id, user_id, **fields):
    from uuid import uuid4

    doc = {"piece_id": str(uuid4()), "workspace_id": ws_id, "user_id": user_id, "content": "A post.", "deleted": False, "created_at": NOW,
           "platform": "LinkedIn", "approval_status": "approved", **fields}
    await content_pieces.insert_one(doc)
    return doc["piece_id"]


async def _campaign_doc(ws_id, user_id, **fields):
    from uuid import uuid4

    doc = {"id": str(uuid4()), "workspace_id": ws_id, "brand_id": "b1", "name": "Launch", "kind": "campaign", "topic_cluster": "A launch", "status": "completed",
           "platforms": ["LinkedIn"], "cadence": {"frequency": "manual", "days_per_batch": 3}, "created_by": user_id, "last_generated_at": NOW - timedelta(days=5),
           "deleted": False, "created_at": NOW, "updated_at": NOW, **fields}
    await get_campaigns_collection().insert_one(doc)
    return doc["id"]


async def _published(ws_id, user_id, hours_ago=3, **fields):
    return await _post(ws_id, user_id, publish_status="published", published_at=NOW - timedelta(hours=hours_ago),
                       publish_scheduled_at=NOW - timedelta(hours=hours_ago + 1), **fields)


async def test_the_owner_and_the_campaign_maker_are_told_once_when_everything_scheduled_has_gone_out(signup_user, told):
    _, profile, ws_id = await _workspace(signup_user)
    campaign_id = await _campaign_doc(ws_id, "someone-who-made-it")
    await _published(ws_id, profile["id"])

    assert await queue_empty.check_workspace(ws_id, NOW) == "notified"
    assert sorted(r["member_user_id"] for r in told.rows) == sorted([profile["id"], "someone-who-made-it"])
    row = told.rows[0]
    assert row["category"] == "queue_empty" and row["visibility"] == "member" and row["href"] == f"/dashboard/campaigns/{campaign_id}?nextBatch=1"
    assert row["title"]["$t"] == "Your queue is empty" and "Launch" in row["description"]["$c"]["name"]
    assert row["metadata"] == {"event": "queue.empty", "campaign_id": campaign_id}

    told.rows.clear()
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: already told for this empty period"
    assert told.rows == [], "once for each empty period"

    await _published(ws_id, profile["id"], hours_ago=2)  # something new was scheduled and went out: a new empty period
    assert await queue_empty.check_workspace(ws_id, NOW) == "notified"


async def test_a_workspace_with_no_campaign_is_told_its_schedule_is_empty_and_pointed_at_a_new_one(signup_user, told):
    _, profile, ws_id = await _workspace(signup_user)
    await _published(ws_id, profile["id"])
    assert await queue_empty.check_workspace(ws_id, NOW) == "notified"
    assert [r["member_user_id"] for r in told.rows] == [profile["id"]], "owner and poster are the same person: one note"
    assert told.rows[0]["title"]["$t"] == "Nothing is scheduled any more" and told.rows[0]["href"] == "/dashboard/pipelines/new"


async def test_nothing_is_sent_while_anything_is_still_upcoming(signup_user, told):
    _, profile, ws_id = await _workspace(signup_user)
    await _published(ws_id, profile["id"])
    queued = await _post(ws_id, profile["id"], publish_status="queued", publish_scheduled_at=NOW + timedelta(days=1))
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: something is upcoming"
    await content_pieces.delete_one({"piece_id": queued})

    planned = await _post(ws_id, profile["id"], intended_publish_at=NOW + timedelta(days=2))
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: something is upcoming"
    await content_pieces.delete_one({"piece_id": planned})

    auto = await _campaign_doc(ws_id, profile["id"], status="active", cadence={"frequency": "weekly", "days_per_batch": 3})
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: something is upcoming"
    await get_campaigns_collection().delete_one({"id": auto})
    assert await queue_empty.check_workspace(ws_id, NOW) == "notified"
    assert told.rows


async def test_it_stays_quiet_when_it_would_be_noise(signup_user, told, monkeypatch):
    _, profile, ws_id = await _workspace(signup_user)
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: nothing published on a schedule lately"
    await _post(ws_id, profile["id"], publish_status="published", published_at=NOW - timedelta(days=45), publish_scheduled_at=NOW - timedelta(days=45))
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: nothing published on a schedule lately", "too long ago"

    await _published(ws_id, profile["id"], hours_ago=0)
    assert (await queue_empty.check_workspace(ws_id, NOW)).startswith("skipped: the last post only just went out")
    await content_pieces.delete_many({"workspace_id": ws_id, "published_at": {"$gt": NOW - timedelta(hours=1)}})
    await _published(ws_id, profile["id"], hours_ago=5)

    await workspaces.update_one({"id": ws_id}, {"$set": {"queue_empty_notice": False}})
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: switched off"
    await workspaces.update_one({"id": ws_id}, {"$unset": {"queue_empty_notice": ""}})

    muted = await _campaign_doc(ws_id, profile["id"], remind_when_empty=False)
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: muted"
    await get_campaigns_collection().delete_one({"id": muted})

    monkeypatch.setattr(queue_empty, "_spare_credits", AsyncMock(return_value=False))
    assert await queue_empty.check_workspace(ws_id, NOW) == "skipped: plan on hold or no credits"
    assert told.rows == []
    assert await queue_empty.check_workspace("no-such-workspace", NOW) == "skipped: no workspace"


async def test_a_muted_campaign_is_not_offered_but_another_one_is(signup_user, told):
    _, profile, ws_id = await _workspace(signup_user)
    await _published(ws_id, profile["id"])
    await _campaign_doc(ws_id, profile["id"], name="Muted", remind_when_empty=False, last_generated_at=NOW - timedelta(days=1))
    other = await _campaign_doc(ws_id, profile["id"], name="Older", last_generated_at=NOW - timedelta(days=9))
    # the most recent campaign is the muted one, so the offer is not made about it
    assert (await queue_empty.check_workspace(ws_id, NOW)) == "skipped: muted"
    await get_campaigns_collection().update_one({"id": other}, {"$set": {"last_generated_at": NOW}})
    assert await queue_empty.check_workspace(ws_id, NOW) == "notified"
    assert told.rows[0]["target_id"] == other


async def test_the_nightly_run_counts_the_workspaces_it_told_and_one_bad_workspace_does_not_stop_it(signup_user, told, monkeypatch):
    _, profile, ws_id = await _workspace(signup_user)
    await _published(ws_id, profile["id"])
    real = queue_empty.check_workspace

    async def flaky(workspace_id, now):
        if workspace_id != ws_id:
            raise RuntimeError("a broken workspace")
        return await real(workspace_id, now)

    monkeypatch.setattr(queue_empty, "check_workspace", flaky)
    assert await queue_empty.notify_empty_queues() >= 1


# ---- the next batch --------------------------------------------------------------------------------------------------------------------
async def test_the_next_batch_preview_shows_the_same_settings_the_credits_and_what_blocks_it(api_client, mock_llm):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Batch WS", tier="large")
    headers = {"X-Workspace-Id": ws_id}
    brand_id = await _create_brand(api_client, ws_id)
    body = _valid_body(brand_id, platforms=["LinkedIn", "Instagram"], cadence={"frequency": "manual", "days_per_batch": 3})
    created = await api_client.post("/api/v1/campaigns/", json=body, headers=headers)
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]

    res = await api_client.get(f"/api/v1/campaigns/{campaign_id}/next-batch", headers=headers)
    assert res.status_code == 200, res.text
    plan = res.json()
    assert plan["days"] == 3 and plan["posts"] == 6 and sorted(plan["platforms"]) == ["Instagram", "LinkedIn"]
    assert plan["planning_credits"] == 5 and plan["writing_credits"] == 6 and plan["credits"] == 5 + 6 + plan["picture_credits"]
    assert plan["can_start"] is True and plan["blockers"] == []
    assert isinstance(plan["warnings"], list)
    assert (await api_client.get("/api/v1/campaigns/nope/next-batch", headers=headers)).status_code == 404

    # nothing was made or charged by looking
    assert await content_pieces.count_documents({"workspace_id": ws_id, "campaign_id": campaign_id}) == 0

    # a batch already going blocks a second one
    started = await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs", headers=headers)
    assert started.status_code == 202, started.text
    again = (await api_client.get(f"/api/v1/campaigns/{campaign_id}/next-batch", headers=headers)).json()
    assert again["can_start"] is False and any("already making a batch" in b for b in again["blockers"])
    await _wait_for(api_client, started.json()["id"], ("done", "failed", "cancelled"), headers=headers)


async def test_a_campaign_can_be_muted_and_the_workspace_switch_is_saved(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Mute WS", tier="large")
    headers = {"X-Workspace-Id": ws_id}
    brand_id = await _create_brand(api_client, ws_id)
    campaign = (await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id), headers=headers)).json()
    assert campaign["remind_when_empty"] is True
    muted = await api_client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"remind_when_empty": False}, headers=headers)
    assert muted.status_code == 200 and muted.json()["remind_when_empty"] is False
    back = await api_client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"remind_when_empty": True}, headers=headers)
    assert back.json()["remind_when_empty"] is True

    off = await api_client.patch(f"/api/v1/workspaces/{ws_id}", json={"queue_empty_notice": False})
    assert off.status_code == 200 and off.json()["policy"]["queue_empty_notice"] is False
    on = await api_client.patch(f"/api/v1/workspaces/{ws_id}", json={"queue_empty_notice": True})
    assert on.json()["policy"]["queue_empty_notice"] is True


def test_the_job_is_in_the_morning_schedule():
    from app.workers import jobs as scheduled

    job = next(j for j in scheduled.JOBS if j.name == "queue_empty")
    assert job.hour == frozenset({6}) and job.minute == frozenset({17})
