"""Any action as a background job: answers at once, one at a time, permissions checked, temporary errors retried, safe repeats come
back after a restart, results capped, internal fields hidden. Some tests register small actions of their own."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from app.db.mongo import content_pieces, pipeline_runs
from app.shared import jobs
from app.shared import pipeline_runs as runs
from tests.conftest import create_workspace
from tests.test_pipeline_runs import _wait_for
from tests.test_regenerate import _create_brand, _seed_piece


class Nothing(BaseModel):
    note: str = ""


@pytest.fixture
def test_actions():
    created = []

    def add(name, run, **kw):
        action = jobs.JobAction(name=name, permission=kw.pop("permission", "edit_content"), payload_model=Nothing, run=run,
                                title=lambda p: f"Test {name}", kind="text", **kw)
        jobs.register(action)
        created.append(name)
        return action

    yield add
    for name in created:
        jobs.ACTIONS.pop(name, None)


async def _ws(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Jobs WS")
    return client, profile, ws_id, {"X-Workspace-Id": ws_id}


async def test_the_actions_can_be_listed(signup_user):
    client, _, _, headers = await _ws(signup_user)
    res = await client.get("/api/v1/jobs/actions", headers=headers)
    assert res.status_code == 200, res.text
    names = {a["action"] for a in res.json()["actions"]}
    assert {"content.bulk_delete", "content.bulk_archive", "text.repurpose", "text.regenerate", "text.generate", "text.batch"} <= names


async def test_a_bulk_delete_starts_at_once_runs_in_the_background_and_reports_what_it_did(signup_user):
    client, profile, ws_id, headers = await _ws(signup_user)
    brand_id = await _create_brand(client, ws_id)
    ids = [await _seed_piece(ws_id, profile["id"], brand_id) for _ in range(3)]

    res = await client.post("/api/v1/jobs", json={"action": "content.bulk_delete", "payload": {"piece_ids": [*ids, "missing-one"]}}, headers=headers)

    assert res.status_code == 202, res.text
    run = res.json()
    assert run["status"] in ("queued", "running") and run["title"] == "Delete 4 posts" and run["steps_total"] == 4
    assert "payload" not in run and "idem_key" not in run                   # internal fields are never sent out
    done = await _wait_for(client, run["id"], ("done", "failed"), headers=headers)
    assert done["status"] == "done", done
    assert done["result"]["data"] == {"deleted": 3, "not_found": ["missing-one"]}
    assert await content_pieces.count_documents({"piece_id": {"$in": ids}, "deleted": True}) == 3
    listed = (await client.get("/api/v1/runs", headers=headers)).json()["runs"]
    assert all("payload" not in r and "idem_key" not in r for r in listed)


async def test_bulk_archive(signup_user):
    client, profile, ws_id, headers = await _ws(signup_user)
    brand_id = await _create_brand(client, ws_id)
    ids = [await _seed_piece(ws_id, profile["id"], brand_id) for _ in range(2)]

    run = (await client.post("/api/v1/jobs", json={"action": "content.bulk_archive", "payload": {"piece_ids": ids}}, headers=headers)).json()
    done = await _wait_for(client, run["id"], ("done", "failed"), headers=headers)
    assert done["status"] == "done" and done["result"]["data"]["archived"] == 2
    assert await content_pieces.count_documents({"piece_id": {"$in": ids}, "archived": True}) == 2


async def test_the_same_job_started_twice_at_once_is_one_job(signup_user, test_actions):
    client, _, _, headers = await _ws(signup_user)

    async def slow(ctx, payload, reporter):
        await asyncio.sleep(1.5)
        return {"ok": True}

    test_actions("test.slow", slow)
    first = await client.post("/api/v1/jobs", json={"action": "test.slow", "payload": {"note": "a"}}, headers=headers)
    second = await client.post("/api/v1/jobs", json={"action": "test.slow", "payload": {"note": "a"}}, headers=headers)
    other = await client.post("/api/v1/jobs", json={"action": "test.slow", "payload": {"note": "b"}}, headers=headers)

    assert first.status_code == 202 and first.json()["already_running"] is False
    assert second.status_code == 200 and second.json()["already_running"] is True and second.json()["id"] == first.json()["id"]
    assert other.json()["id"] != first.json()["id"]                          # a different payload is a different job
    await _wait_for(client, first.json()["id"], ("done", "failed"), headers=headers)
    await _wait_for(client, other.json()["id"], ("done", "failed"), headers=headers)
    again = await client.post("/api/v1/jobs", json={"action": "test.slow", "payload": {"note": "a"}}, headers=headers)
    assert again.json()["id"] != first.json()["id"]                          # once finished, the same request starts a new one
    await _wait_for(client, again.json()["id"], ("done", "failed"), headers=headers)


async def test_a_client_key_stops_a_retry_from_starting_a_second_job(signup_user, test_actions):
    client, _, _, headers = await _ws(signup_user)

    async def slow(ctx, payload, reporter):
        await asyncio.sleep(1.2)

    test_actions("test.keyed", slow)
    a = await client.post("/api/v1/jobs", json={"action": "test.keyed", "payload": {"note": "1"}}, headers={**headers, "Idempotency-Key": "k-1"})
    b = await client.post("/api/v1/jobs", json={"action": "test.keyed", "payload": {"note": "2"}}, headers={**headers, "Idempotency-Key": "k-1"})
    assert b.json()["id"] == a.json()["id"] and b.json()["already_running"] is True
    await _wait_for(client, a.json()["id"], ("done", "failed"), headers=headers)


async def test_unknown_actions_and_bad_payloads_are_refused_before_any_run_is_made(signup_user):
    client, _, _, headers = await _ws(signup_user)
    assert (await client.post("/api/v1/jobs", json={"action": "nope.nothing", "payload": {}}, headers=headers)).status_code == 404
    bad = await client.post("/api/v1/jobs", json={"action": "content.bulk_delete", "payload": {"piece_ids": []}}, headers=headers)
    assert bad.status_code == 422
    too_many = await client.post("/api/v1/jobs", json={"action": "content.bulk_delete", "payload": {"piece_ids": [str(i) for i in range(201)]}}, headers=headers)
    assert too_many.status_code == 422
    assert (await client.get("/api/v1/runs", headers=headers)).json()["runs"] == []


async def test_a_member_without_the_permission_cannot_start_it(signup_user):
    client, profile, ws_id, _ = await _ws(signup_user)
    ctx = await jobs.build_context(ws_id, profile["id"])
    ctx.member = {**ctx.member, "role": "viewer"}
    with pytest.raises(HTTPException) as caught:
        await jobs.submit(action_name="content.bulk_delete", payload={"piece_ids": ["a"]}, ctx=ctx)
    assert caught.value.status_code == 403


async def test_a_permission_removed_after_submitting_is_respected_when_the_work_starts(signup_user, test_actions):
    client, profile, ws_id, headers = await _ws(signup_user)
    ran = []

    async def work(ctx, payload, reporter):
        ran.append(1)

    test_actions("test.recheck", work)
    original = jobs.build_context

    async def demoted(workspace_id, user_id):
        ctx = await original(workspace_id, user_id)
        ctx.member = {**ctx.member, "role": "viewer"}
        return ctx

    jobs.build_context = demoted
    try:
        run = (await client.post("/api/v1/jobs", json={"action": "test.recheck", "payload": {}}, headers=headers)).json()
        done = await _wait_for(client, run["id"], ("done", "failed"), headers=headers)
    finally:
        jobs.build_context = original
    assert done["status"] == "failed" and ran == []
    assert "permission" in done["error"].lower()


async def test_temporary_errors_are_retried_then_the_job_finishes(signup_user, test_actions, monkeypatch):
    client, _, _, headers = await _ws(signup_user)
    monkeypatch.setattr(jobs, "RETRY_WAITS", (0.05, 0.05, 0.05))
    calls = {"n": 0}

    async def flaky(ctx, payload, reporter):
        calls["n"] += 1
        if calls["n"] < 3:
            raise HTTPException(status_code=503, detail="The model is busy.")
        return {"attempts": calls["n"]}

    test_actions("test.flaky", flaky, retries=2)
    run = (await client.post("/api/v1/jobs", json={"action": "test.flaky", "payload": {}}, headers=headers)).json()
    done = await _wait_for(client, run["id"], ("done", "failed"), headers=headers)
    assert done["status"] == "done" and done["result"]["data"] == {"attempts": 3}


async def test_a_real_error_is_not_retried_and_a_temporary_one_gives_up_after_its_attempts(signup_user, test_actions, monkeypatch):
    client, _, _, headers = await _ws(signup_user)
    monkeypatch.setattr(jobs, "RETRY_WAITS", (0.05, 0.05, 0.05))
    calls = {"bad": 0, "busy": 0}

    async def bad(ctx, payload, reporter):
        calls["bad"] += 1
        raise ValueError("That page could not be read.")

    async def busy(ctx, payload, reporter):
        calls["busy"] += 1
        raise HTTPException(status_code=503, detail="The model is busy.")

    test_actions("test.bad", bad, retries=2)
    test_actions("test.busy", busy, retries=2)
    r1 = (await client.post("/api/v1/jobs", json={"action": "test.bad", "payload": {}}, headers=headers)).json()
    r2 = (await client.post("/api/v1/jobs", json={"action": "test.busy", "payload": {}}, headers=headers)).json()
    d1 = await _wait_for(client, r1["id"], ("done", "failed"), headers=headers)
    d2 = await _wait_for(client, r2["id"], ("done", "failed"), headers=headers)
    assert d1["status"] == "failed" and calls["bad"] == 1
    assert d2["status"] == "failed" and calls["busy"] == 3 and "busy" in d2["error"].lower()


async def test_a_cancel_stops_the_work_and_is_reported_as_cancelled(signup_user, test_actions):
    client, _, _, headers = await _ws(signup_user)
    steps = {"n": 0}

    async def stepwise(ctx, payload, reporter):
        for i in range(60):
            await reporter.step(f"Step {i}")
            steps["n"] += 1
            await asyncio.sleep(0.2)

    test_actions("test.steps", stepwise)
    run = (await client.post("/api/v1/jobs", json={"action": "test.steps", "payload": {}}, headers=headers)).json()
    await _wait_for(client, run["id"], ("running",), headers=headers)
    await asyncio.sleep(0.8)
    assert (await client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)).status_code == 200
    ended = await _wait_for(client, run["id"], ("cancelled", "done", "failed"), headers=headers)
    assert ended["status"] == "cancelled" and steps["n"] < 30


async def test_a_safe_to_repeat_job_is_started_again_after_a_restart_and_others_are_closed(signup_user, test_actions):
    client, profile, ws_id, headers = await _ws(signup_user)
    ran = {"safe": 0, "unsafe": 0}

    async def safe(ctx, payload, reporter):
        ran["safe"] += 1
        return {"again": True}

    async def unsafe(ctx, payload, reporter):
        ran["unsafe"] += 1

    test_actions("test.safe", safe, restartable=True)
    test_actions("test.unsafe", unsafe, restartable=False)
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    base = {"workspace_id": ws_id, "created_by": profile["id"], "kind": "text", "title": "Interrupted", "status": "running",
            "steps_done": 1, "steps_total": 3, "ref": {}, "href": None, "result": None, "error": None,
            "pause_requested": False, "cancel_requested": False, "created_at": old, "updated_at": old, "started_at": old,
            "finished_at": None, "paused_at": None, "heartbeat_at": old, "last_reminded_at": None, "reminders_sent": 0}
    await pipeline_runs.insert_one({**base, "id": "safe-run", "action": "test.safe", "restartable": True, "restart_attempts": 0, "payload": {"note": ""}})
    await pipeline_runs.insert_one({**base, "id": "unsafe-run", "action": "test.unsafe", "restartable": False, "restart_attempts": 0})
    await pipeline_runs.insert_one({**base, "id": "spent-run", "action": "test.safe", "restartable": True, "restart_attempts": jobs.MAX_RESTARTS, "payload": {"note": ""}})

    started = await jobs.resume_interrupted()
    closed = await runs.fail_interrupted()

    assert started >= 1
    assert closed >= 2
    done = await _wait_for(client, "safe-run", ("done", "failed"), headers=headers)
    assert done["status"] == "done" and ran == {"safe": 1, "unsafe": 0}
    assert (await pipeline_runs.find_one({"id": "safe-run"}))["restart_attempts"] == 1
    assert (await pipeline_runs.find_one({"id": "unsafe-run"}))["status"] == "failed"
    assert (await pipeline_runs.find_one({"id": "spent-run"}))["status"] == "failed"      # tried twice already: not a third time


def test_a_result_that_is_too_large_is_replaced_by_a_summary():
    assert jobs.make_result({"a": 1}) == {"data": {"a": 1}}
    huge = jobs.make_result(["x" * 1000 for _ in range(600)])
    assert huge["truncated"] is True and huge["data"] is None and "600 items" in huge["note"]


def test_only_temporary_errors_are_worth_a_retry():
    assert jobs.is_temporary(HTTPException(status_code=503, detail="x"))
    assert jobs.is_temporary(HTTPException(status_code=429, detail="x"))
    assert jobs.is_temporary(asyncio.TimeoutError())
    assert not jobs.is_temporary(HTTPException(status_code=400, detail="x"))
    assert not jobs.is_temporary(HTTPException(status_code=403, detail="x"))
    assert not jobs.is_temporary(ValueError("x"))


def test_the_duplicate_key_depends_on_who_what_and_with_which_data():
    a = jobs.idempotency_key("w", "u", "act", {"x": 1})
    assert a == jobs.idempotency_key("w", "u", "act", {"x": 1})
    assert a != jobs.idempotency_key("w", "u", "act", {"x": 2})
    assert a != jobs.idempotency_key("w", "other", "act", {"x": 1})
    assert a != jobs.idempotency_key("w2", "u", "act", {"x": 1})
    assert jobs.idempotency_key("w", "u", "act", {"x": 1}, client_key="k") == jobs.idempotency_key("w", "u", "act", {"x": 9}, client_key="k")
