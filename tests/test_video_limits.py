"""Render minutes and storage: what a plan allows, what a workspace has used, and the refusal when a render would pass the month."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db.mongo import get_db, media_assets
from app.video import jobs, limits
from tests.test_video_worker_routes import INPUTS, _edit


async def _workspace(tier: str) -> str:
    ws = f"ws-{uuid4()}"
    await get_db()["workspaces"].insert_one({"id": ws, "tier": tier})
    return ws


async def _job(ws: str, seconds: float, status: str, **extra) -> None:
    await get_db()["video_jobs"].insert_one({"id": uuid4().hex, "workspace_id": ws, "status": status, "duration_s": seconds,
                                              "created_at": datetime.now(timezone.utc), **extra})


def test_every_plan_has_limits_and_an_unknown_plan_gets_the_largest():
    assert limits.limits_for({"tier": "single"})["render_minutes"] < limits.limits_for({"tier": "duo"})["render_minutes"]
    assert limits.limits_for({"tier": "mystery"}) == limits.TIER_LIMITS["large"]
    assert limits.limits_for(None) == limits.TIER_LIMITS["large"]


async def test_the_meters_count_finished_and_waiting_renders_but_not_failed_ones_or_previews():
    ws = await _workspace("single")
    await _job(ws, 600, "done")
    await _job(ws, 300, "queued")
    await _job(ws, 900, "failed")
    await _job(ws, 900, "done", kind="proxy")
    await media_assets.insert_one({"id": uuid4().hex, "workspace_id": ws, "kind": "video", "size_bytes": 50_000_000, "url": "https://x/v.mp4"})

    meters = await limits.usage(ws)

    assert meters["render_minutes"]["used"] == 15.0 and meters["render_minutes"]["limit"] == 120 and meters["render_minutes"]["state"] == "ok"
    assert meters["storage_mb"]["used"] == 50.0


async def test_a_meter_turns_near_at_eighty_percent_and_over_at_the_limit():
    ws = await _workspace("single")
    await _job(ws, 100 * 60, "done")
    assert (await limits.usage(ws))["render_minutes"]["state"] == "near"
    await _job(ws, 25 * 60, "done")
    assert (await limits.usage(ws))["render_minutes"]["state"] == "over"


async def test_a_project_render_that_would_pass_the_month_is_refused_in_plain_words():
    ws = await _workspace("single")
    await _job(ws, 119 * 60, "done")

    with pytest.raises(jobs.JobError) as err:
        await jobs.enqueue(workspace_id=ws, user_id="u1", inputs=INPUTS, edit=_edit(seconds=120), project_id="p1")

    assert err.value.status == 429 and "render minutes" in err.value.message and "1st" in err.value.message


async def test_the_same_render_asked_again_is_not_counted_twice_or_refused():
    ws = await _workspace("single")
    first = await jobs.enqueue(workspace_id=ws, user_id="u1", inputs=INPUTS, edit=_edit(seconds=60), project_id="p1", idem_key="k1")
    await _job(ws, 118 * 60, "done")                                  # now the month is nearly used

    again = await jobs.enqueue(workspace_id=ws, user_id="u1", inputs=INPUTS, edit=_edit(seconds=60), project_id="p1", idem_key="k1")

    assert again["id"] == first["id"]


async def test_a_render_not_tied_to_a_project_is_held_to_the_monthly_limit_too():
    ws = await _workspace("single")
    await _job(ws, 200 * 60, "done")
    with pytest.raises(jobs.JobError) as refused:
        await jobs.enqueue(workspace_id=ws, user_id="u1", inputs=INPUTS, edit=_edit(seconds=10))
    assert refused.value.status == 429


async def test_an_upload_that_does_not_fit_the_storage_allowance_is_refused_before_it_starts(signup_user):
    from tests.conftest import create_workspace

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Full {uuid4().hex[:6]}")
    await get_db()["workspaces"].update_one({"id": ws}, {"$set": {"tier": "single"}})
    await media_assets.insert_one({"id": uuid4().hex, "workspace_id": ws, "kind": "video", "size_bytes": 2_040_000_000, "url": "https://x/v.mp4"})

    refused = await client.post("/api/v1/video-media/uploads", headers={"X-Workspace-Id": ws},
                                json={"filename": "talk.mp4", "content_type": "video/mp4", "bytes": 50_000_000})

    assert refused.status_code == 413 and "does not fit" in refused.json()["detail"] and "2048 MB" in refused.json()["detail"]
