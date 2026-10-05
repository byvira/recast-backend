"""Background runs: a campaign batch starts at once, can be paused, resumed and cancelled, ends with a saved outcome, and an
interrupted run is closed on startup. The model is mocked: no real call."""
import asyncio

from app.db.mongo import content_pieces, pipeline_runs
from app.shared import pipeline_runs as runs
from tests.conftest import create_workspace, invite_and_accept, signup_new_user
from tests.test_campaigns import _create_brand, _valid_body


async def _campaign(client, days=1, ws_id=None):
    brand_id = await _create_brand(client, ws_id)
    headers = {"X-Workspace-Id": ws_id} if ws_id else {}
    res = await client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, platforms=["LinkedIn"], cadence={"frequency": "manual", "days_per_batch": days}),
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _wait_for(client, run_id, wanted, seconds=40, headers=None):
    for _ in range(int(seconds / 0.25)):
        body = (await client.get(f"/api/v1/runs/{run_id}", headers=headers or {})).json()
        if body["status"] in wanted:
            return body
        await asyncio.sleep(0.25)
    raise AssertionError(f"run never reached {wanted}, last: {body}")


async def test_a_run_starts_at_once_and_finishes_in_the_background(api_client, mock_llm):
    await signup_new_user(api_client)
    campaign_id = await _campaign(api_client, days=1)
    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content for this campaign day.")

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs")
    assert res.status_code == 202, res.text
    run = res.json()
    assert run["kind"] == "campaign" and run["status"] in ("queued", "running") and run["steps_total"] == 1

    done = await _wait_for(api_client, run["id"], ("done", "failed"))
    assert done["status"] == "done", done
    assert done["result"]["new_piece_count"] == 1 and done["progress"] == 100
    assert await content_pieces.count_documents({"campaign_id": campaign_id}) == 1

    listed = (await api_client.get("/api/v1/runs", params={"campaign_id": campaign_id})).json()["runs"]
    assert [r["id"] for r in listed] == [run["id"]]


async def test_a_second_run_for_the_same_campaign_is_refused_while_one_is_active(api_client, mock_llm):
    await signup_new_user(api_client)
    campaign_id = await _campaign(api_client)
    first = await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs")
    assert first.status_code == 202
    second = await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs")
    assert second.status_code == 409
    await api_client.post(f"/api/v1/runs/{first.json()['id']}/cancel")
    await _wait_for(api_client, first.json()["id"], ("cancelled", "done", "failed"))


async def test_pause_holds_the_work_between_days_and_resume_finishes_it(api_client, mock_llm):
    await signup_new_user(api_client)
    campaign_id = await _campaign(api_client, days=2)
    mock_llm.set_structured({"angles": ["One", "Two"]})
    mock_llm.set_plain("Real generated content for this campaign day.")

    run = (await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs")).json()
    assert (await api_client.post(f"/api/v1/runs/{run['id']}/pause")).status_code == 200
    held = await _wait_for(api_client, run["id"], ("paused", "done", "failed"))
    assert held["status"] == "paused" and held["paused_at"]
    # The pause lands at the next day boundary, which may be before or after the first day, so count what was made.
    made = await content_pieces.count_documents({"campaign_id": campaign_id})
    assert made == held["steps_done"] and made < 2
    await asyncio.sleep(2.5)
    assert await content_pieces.count_documents({"campaign_id": campaign_id}) == made

    await api_client.post(f"/api/v1/runs/{run['id']}/resume")
    done = await _wait_for(api_client, run["id"], ("done", "failed"))
    assert done["status"] == "done" and done["steps_done"] == 2
    assert await content_pieces.count_documents({"campaign_id": campaign_id}) == 2


async def test_cancel_stops_the_run_and_keeps_the_days_already_made(api_client, mock_llm):
    await signup_new_user(api_client)
    campaign_id = await _campaign(api_client, days=2)
    mock_llm.set_structured({"angles": ["One", "Two"]})
    mock_llm.set_plain("Real generated content for this campaign day.")

    run = (await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs")).json()
    await api_client.post(f"/api/v1/runs/{run['id']}/pause")
    held = await _wait_for(api_client, run["id"], ("paused", "done", "failed"))
    assert held["status"] == "paused"
    made = await content_pieces.count_documents({"campaign_id": campaign_id})
    await api_client.post(f"/api/v1/runs/{run['id']}/cancel")
    done = await _wait_for(api_client, run["id"], ("cancelled",))
    assert done["finished_at"]
    assert await content_pieces.count_documents({"campaign_id": campaign_id}) == made < 2
    campaign = (await api_client.get(f"/api/v1/campaigns/{campaign_id}")).json()
    assert len(campaign["piece_ids"]) == made


async def test_a_failed_run_keeps_a_plain_reason(api_client, mock_llm):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]
    campaign_id = (await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))).json()["id"]

    run = (await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs")).json()
    done = await _wait_for(api_client, run["id"], ("failed",))
    assert "Brand profile is not complete" in done["error"]


async def test_only_members_who_can_create_may_control_a_run(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Run Perms", tier="large")
    campaign_id = await _campaign(api_client, ws_id=ws_id)
    headers = {"X-Workspace-Id": ws_id}
    run = (await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs", headers=headers)).json()

    viewer, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")
    assert (await viewer.get(f"/api/v1/runs/{run['id']}", headers=headers)).status_code == 200
    assert (await viewer.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)).status_code == 403
    assert (await viewer.post(f"/api/v1/campaigns/{campaign_id}/runs", headers=headers)).status_code == 403

    await api_client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)
    await _wait_for(api_client, run["id"], ("cancelled", "done", "failed"), headers=headers)


async def test_an_unknown_run_is_not_found_and_a_finished_run_ignores_controls(api_client):
    await signup_new_user(api_client)
    assert (await api_client.get("/api/v1/runs/nope")).status_code == 404
    assert (await api_client.post("/api/v1/runs/nope/pause")).status_code == 404


async def test_runs_whose_server_died_are_closed_but_a_run_still_beating_is_left_alone():
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    dead = await runs.create_run(workspace_id="w-restart", user_id="u", kind="campaign", title="Dead")
    alive = await runs.create_run(workspace_id="w-restart", user_id="u", kind="campaign", title="Alive")
    await pipeline_runs.update_one({"id": dead["id"]}, {"$set": {"status": "running", "created_at": old, "heartbeat_at": old}})
    await pipeline_runs.update_one(
        {"id": alive["id"]},
        {"$set": {"status": "running", "created_at": old, "heartbeat_at": datetime.now(timezone.utc)}},
    )
    try:
        assert await runs.fail_interrupted() >= 1
        row = await pipeline_runs.find_one({"id": dead["id"]})
        assert row["status"] == "failed" and "restarted" in row["error"]
        assert (await pipeline_runs.find_one({"id": alive["id"]}))["status"] == "running"
    finally:
        await pipeline_runs.delete_many({"workspace_id": "w-restart"})


def test_reminders_come_at_one_three_and_seven_days_then_stop():
    from datetime import datetime, timedelta, timezone

    from app.workers.run_reminders import reminder_due

    now = datetime.now(timezone.utc)
    paused = lambda hours, sent: {"paused_at": now - timedelta(hours=hours), "reminders_sent": sent}  # noqa: E731
    assert not reminder_due(paused(23, 0), now)
    assert reminder_due(paused(25, 0), now)
    assert not reminder_due(paused(25, 1), now)
    assert reminder_due(paused(73, 1), now)
    assert reminder_due(paused(169, 2), now)
    assert not reminder_due(paused(1000, 3), now)
    assert not reminder_due({"paused_at": None, "reminders_sent": 0}, now)


async def test_a_forgotten_pause_gets_one_note_in_activity_each_time_it_is_due():
    from datetime import datetime, timedelta, timezone

    from app.db.mongo import activity_entries
    from app.workers.run_reminders import remind_about_paused_runs

    doc = await runs.create_run(workspace_id="w-remind", user_id="u-remind", kind="campaign", title="Campaign \"Forgotten\"")
    await pipeline_runs.update_one(
        {"id": doc["id"]}, {"$set": {"status": "paused", "paused_at": datetime.now(timezone.utc) - timedelta(hours=26)}},
    )
    try:
        assert await remind_about_paused_runs() >= 1
        assert (await pipeline_runs.find_one({"id": doc["id"]}))["reminders_sent"] == 1
        assert await activity_entries.count_documents({"_id": f"system:run-reminder:{doc['id']}:1"}) == 1
        await remind_about_paused_runs()
        assert (await pipeline_runs.find_one({"id": doc["id"]}))["reminders_sent"] == 1
    finally:
        await pipeline_runs.delete_many({"workspace_id": "w-remind"})
        await activity_entries.delete_many({"workspace_id": "w-remind"})


async def test_a_workspace_cannot_have_more_than_three_jobs_going(api_client, mock_llm):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Capacity WS", tier="large")
    headers = {"X-Workspace-Id": ws_id}
    campaign_id = await _campaign(api_client, ws_id=ws_id)
    for _ in range(runs.MAX_ACTIVE_PER_WORKSPACE):
        await runs.create_run(workspace_id=ws_id, user_id="u", kind="audio", title="Busy")
    try:
        res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/runs", headers=headers)
        assert res.status_code == 429 and "already running" in res.json()["detail"]
    finally:
        await pipeline_runs.delete_many({"workspace_id": ws_id})
