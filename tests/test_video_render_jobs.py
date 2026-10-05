"""Every video render is recorded; the same render cannot be started twice at once; a render lost in a restart shows as interrupted."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import get_db


def _ctx(ws_id: str):
    return SimpleNamespace(workspace_id=ws_id)


def _body():
    return audio_module.MakeVideoRequest()


async def _jobs(ws_id: str, asset_id: str) -> list[dict]:
    return await get_db()["audio_render_jobs"].find({"workspace_id": ws_id, "audio_asset_id": asset_id}).sort("started_at", 1).to_list(length=20)


async def test_a_render_is_recorded_as_running_then_done():
    ws_id, asset_id = f"ws-{uuid4()}", f"a-{uuid4()}"
    guard = audio_module._video_render_guard(asset_id, _body(), _ctx(ws_id))
    await guard.__anext__()

    [job] = await _jobs(ws_id, asset_id)
    assert job["status"] == "running" and job["kind"] == "video"

    with pytest.raises(StopAsyncIteration):
        await guard.__anext__()
    [job] = await _jobs(ws_id, asset_id)
    assert job["status"] == "done" and job["finished_at"] is not None


async def test_the_same_render_cannot_be_started_twice_at_once():
    ws_id, asset_id = f"ws-{uuid4()}", f"a-{uuid4()}"
    first = audio_module._video_render_guard(asset_id, _body(), _ctx(ws_id))
    await first.__anext__()

    second = audio_module._video_render_guard(asset_id, _body(), _ctx(ws_id))
    with pytest.raises(HTTPException) as caught:
        await second.__anext__()
    assert caught.value.status_code == 409
    assert "already being made" in caught.value.detail
    assert len(await _jobs(ws_id, asset_id)) == 1          # the refused click leaves no second record

    with pytest.raises(StopAsyncIteration):
        await first.__anext__()
    # Once the first has finished the same request may start again.
    again = audio_module._video_render_guard(asset_id, _body(), _ctx(ws_id))
    await again.__anext__()
    with pytest.raises(StopAsyncIteration):
        await again.__anext__()


async def test_a_render_that_fails_is_recorded_with_its_reason_and_frees_the_slot():
    ws_id, asset_id = f"ws-{uuid4()}", f"a-{uuid4()}"
    guard = audio_module._video_render_guard(asset_id, _body(), _ctx(ws_id))
    await guard.__anext__()
    with pytest.raises(HTTPException):
        await guard.athrow(HTTPException(status_code=400, detail="The recording is too short."))

    [job] = await _jobs(ws_id, asset_id)
    assert job["status"] == "failed" and job["error"] == "The recording is too short."
    retry = audio_module._video_render_guard(asset_id, _body(), _ctx(ws_id))
    await retry.__anext__()
    with pytest.raises(StopAsyncIteration):
        await retry.__anext__()


async def test_a_render_lost_in_a_restart_shows_as_interrupted_when_the_list_is_read():
    ws_id, asset_id = f"ws-{uuid4()}", f"a-{uuid4()}"
    db = get_db()["audio_render_jobs"]
    await db.insert_one({"id": "old", "workspace_id": ws_id, "audio_asset_id": asset_id, "kind": "video", "status": "running",
                         "started_at": datetime.now(timezone.utc) - timedelta(hours=3)})
    await db.insert_one({"id": "new", "workspace_id": ws_id, "audio_asset_id": asset_id, "kind": "video", "status": "running",
                         "started_at": datetime.now(timezone.utc) - timedelta(minutes=2)})

    result = await audio_module.list_render_jobs(asset_id, _ctx(ws_id))

    by_id = {j["id"]: j for j in result["jobs"]}
    assert by_id["old"]["status"] == "failed" and "interrupted" in by_id["old"]["error"]
    assert by_id["new"]["status"] == "running"
