"""The Home pipeline cards: how long runs take and how many worked come from the saved runs of the last 30 days."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import pipeline_runs
from tests.conftest import create_workspace


async def _run(ws_id: str, kind: str, status: str, *, seconds: float = 10, days_ago: float = 1) -> None:
    finished = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await pipeline_runs.insert_one({
        "id": str(uuid4()), "workspace_id": ws_id, "created_by": "u", "kind": kind, "title": "t", "status": status,
        "started_at": finished - timedelta(seconds=seconds), "finished_at": finished, "created_at": finished, "updated_at": finished,
        "steps_done": 1, "steps_total": 1, "ref": {}, "result": None, "error": None,
    })


async def test_the_summary_has_average_time_and_success_rate_per_kind(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Summary Runs WS")
    for seconds in (10, 20, 30):
        await _run(ws_id, "image", "done", seconds=seconds)
    await _run(ws_id, "image", "failed", seconds=5)
    await _run(ws_id, "image", "cancelled")            # the member's choice: counts in neither
    await _run(ws_id, "audio", "done", seconds=60)
    await _run(ws_id, "image", "done", seconds=999, days_ago=45)   # too old

    res = await client.get("/api/v1/analytics/pipeline-summary", headers={"X-Workspace-Id": ws_id})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["image"]["runs_30d"] == 4 and body["image"]["success_rate"] == 75.0 and body["image"]["avg_seconds"] == 20.0
    assert body["audio"]["runs_30d"] == 1 and body["audio"]["success_rate"] == 100.0 and body["audio"]["avg_seconds"] == 60.0
    assert body["video"]["runs_30d"] == 0 and body["video"]["success_rate"] is None and body["video"]["avg_seconds"] is None
    assert "total" in body["image"] and "pending_approval" in body["image"]            # the old fields are unchanged
