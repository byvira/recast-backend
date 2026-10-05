"""Text work started in the background: it answers at once with a run, keeps going after the request, can be paused, resumed and
cancelled between model calls, and ends with a saved result and an Activity Log note. The pipeline is stubbed: no real model call."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.db.mongo import activity_entries
from app.shared import llm
from tests.conftest import create_workspace
from tests.test_pipeline_runs import _wait_for
from tests.test_regenerate import _create_brand, _seed_piece


async def _note_for(ws_id: str, run_id: str):
    """The Activity Log note is written just after the run's status changes, so wait for it a moment."""
    for _ in range(40):
        note = await activity_entries.find_one({"_id": f"system:run:{run_id}"})
        if note:
            return note
        await asyncio.sleep(0.25)
    rows = await activity_entries.find({"workspace_id": ws_id}).to_list(length=20)
    raise AssertionError(f"no Activity note for run {run_id}; rows in the workspace: {[(r['_id'], r.get('title')) for r in rows]}")


def _fake_result():
    return SimpleNamespace(pieces=[SimpleNamespace(
        content="Rewritten in the background.", hooks=[], seo={}, readability_score=60, readability_level="Standard",
    )])


async def _setup(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Background Text WS")
    brand_id = await _create_brand(client, ws_id)
    piece_id = await _seed_piece(ws_id, profile["id"], brand_id)
    return client, ws_id, brand_id, piece_id


def _body(brand_id, piece_id):
    return {"platform": "LinkedIn", "brand_id": brand_id, "piece_id": piece_id, "content": "Some source."}


async def test_regenerate_in_the_background_answers_at_once_and_ends_with_the_result(signup_user):
    client, ws_id, brand_id, piece_id = await _setup(signup_user)
    headers = {"X-Workspace-Id": ws_id}

    async def fake(**kwargs):
        return _fake_result()

    with patch("app.api.v1.text.run_text_pipeline", new=fake):
        res = await client.post("/api/v1/text/regenerate/run", json=_body(brand_id, piece_id), headers=headers)
        assert res.status_code == 202, res.text
        run = res.json()
        assert run["kind"] == "text" and run["status"] in ("queued", "running") and run["title"].startswith("Regenerate")
        done = await _wait_for(client, run["id"], ("done", "failed"), headers=headers)

    assert done["status"] == "done", done
    assert done["result"]["data"]["content"] == "Rewritten in the background."
    note = await _note_for(ws_id, run["id"])
    assert note and "finished" in note["title"]


async def test_a_run_can_be_paused_holds_the_work_and_finishes_after_resume(signup_user):
    client, ws_id, brand_id, piece_id = await _setup(signup_user)
    headers = {"X-Workspace-Id": ws_id}
    progress = {"calls": 0}

    async def fake(**kwargs):
        for _ in range(40):
            await llm._wait_at_run_gate()          # what every model call does first
            progress["calls"] += 1
            await asyncio.sleep(0.3)
        return _fake_result()

    with patch("app.api.v1.text.run_text_pipeline", new=fake):
        run = (await client.post("/api/v1/text/regenerate/run", json=_body(brand_id, piece_id), headers=headers)).json()
        await _wait_for(client, run["id"], ("running",), headers=headers)
        await asyncio.sleep(1.2)
        assert (await client.post(f"/api/v1/runs/{run['id']}/pause", headers=headers)).status_code == 200
        held = await _wait_for(client, run["id"], ("paused", "done", "failed"), headers=headers)
        assert held["status"] == "paused"
        frozen_at = progress["calls"]
        await asyncio.sleep(2.5)
        assert progress["calls"] <= frozen_at + 1, "the work kept going while paused"

        assert (await client.post(f"/api/v1/runs/{run['id']}/resume", headers=headers)).status_code == 200
        done = await _wait_for(client, run["id"], ("done", "failed"), seconds=60, headers=headers)

    assert done["status"] == "done", done
    assert progress["calls"] > frozen_at + 3


async def test_a_run_can_be_cancelled_stops_early_and_is_reported_as_cancelled(signup_user):
    client, ws_id, brand_id, piece_id = await _setup(signup_user)
    headers = {"X-Workspace-Id": ws_id}
    progress = {"calls": 0}

    async def fake(**kwargs):
        for _ in range(60):
            await llm._wait_at_run_gate()
            progress["calls"] += 1
            await asyncio.sleep(0.3)
        return _fake_result()

    with patch("app.api.v1.text.run_text_pipeline", new=fake):
        run = (await client.post("/api/v1/text/regenerate/run", json=_body(brand_id, piece_id), headers=headers)).json()
        await _wait_for(client, run["id"], ("running",), headers=headers)
        await asyncio.sleep(1.2)
        assert (await client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)).status_code == 200
        ended = await _wait_for(client, run["id"], ("cancelled", "done", "failed"), headers=headers)

    assert ended["status"] == "cancelled", ended
    assert progress["calls"] < 30
    note = await _note_for(ws_id, run["id"])
    assert note and "cancelled" in note["title"]


async def test_a_failure_inside_the_work_is_recorded_on_the_run_with_a_plain_reason(signup_user):
    client, ws_id, brand_id, piece_id = await _setup(signup_user)
    headers = {"X-Workspace-Id": ws_id}

    async def fake(**kwargs):
        raise ValueError("That page could not be read.")

    with patch("app.api.v1.text.run_text_pipeline", new=fake):
        run = (await client.post("/api/v1/text/regenerate/run", json=_body(brand_id, piece_id), headers=headers)).json()
        done = await _wait_for(client, run["id"], ("done", "failed"), headers=headers)

    assert done["status"] == "failed"
    assert done["error"]
    note = await _note_for(ws_id, run["id"])
    assert note and "failed" in note["title"]


async def test_an_unknown_brand_is_refused_before_any_run_is_made(signup_user):
    client, ws_id, brand_id, piece_id = await _setup(signup_user)
    headers = {"X-Workspace-Id": ws_id}
    for path, body in (
        ("/api/v1/text/regenerate/run", {"platform": "LinkedIn", "brand_id": "nope", "content": "x"}),
        ("/api/v1/text/repurpose/run", {"source_content": "x", "source_type": "text", "target_platforms": ["LinkedIn"], "brand_id": "nope",
                                        "source_platform": "LinkedIn"}),
        ("/api/v1/text/generate/run", {"content": "x", "source_type": "text", "platforms": ["LinkedIn"], "brand_id": "nope"}),
        ("/api/v1/text/batch/run", {"topic_cluster": "x", "platforms": ["LinkedIn"], "brand_id": "nope", "days": 2}),
    ):
        res = await client.post(path, json=body, headers=headers)
        assert res.status_code == 404, (path, res.status_code, res.text)
    assert (await client.get("/api/v1/runs", headers=headers)).json()["runs"] == []


async def test_the_gate_does_nothing_outside_a_run_and_stops_a_cancelled_one():
    await llm._wait_at_run_gate()                  # no gate set: returns at once
    calls = []

    async def gate():
        calls.append(1)
        raise RuntimeError("cancelled")

    llm.set_run_gate(gate)
    try:
        try:
            await llm._wait_at_run_gate()
        except RuntimeError:
            pass
        assert calls == [1]
    finally:
        llm.set_run_gate(None)
    await llm._wait_at_run_gate()
