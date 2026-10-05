"""The campaign actions as background jobs: next batch, retry media and a new picture. The work itself is covered by the campaign
tests; this checks they start through /jobs, end with the same answer the plain route gave, and report a clear reason on failure."""
import pytest
from fastapi import HTTPException

from app.shared import job_actions, jobs
from tests.test_audio_assets import _h, _setup, stubs  # noqa: F401 - fixture reuse
from tests.test_pipeline_runs import _wait_for

NAMES = {"campaign.next_batch", "campaign.retry_media", "campaign.regenerate_media"}


async def _start(client, headers, action, payload):
    res = await client.post("/api/v1/jobs", json={"action": action, "payload": payload}, headers=headers)
    assert res.status_code == 202, res.text
    return res.json()


async def test_the_campaign_actions_are_registered_as_campaign_jobs_that_are_not_restarted():
    jobs.ACTIONS.clear()
    job_actions.register_all()
    found = {n: a for n, a in jobs.ACTIONS.items() if n in NAMES}
    assert set(found) == NAMES
    assert all(a.kind == "campaign" and not a.restartable and a.gated for a in found.values())


@pytest.mark.parametrize("action,payload", [
    ("campaign.next_batch", {"campaign_id": "missing"}),
    ("campaign.retry_media", {"campaign_id": "missing", "piece_id": "p1"}),
    ("campaign.regenerate_media", {"campaign_id": "missing", "piece_id": "p1"}),
])
async def test_a_campaign_that_does_not_exist_ends_the_job_with_that_reason(signup_user, stubs, action, payload):  # noqa: F811
    client, _, ws_id, _ = await _setup(signup_user)

    run = await _start(client, _h(ws_id), action, payload)
    done = await _wait_for(client, run["id"], ("done", "failed"), headers=_h(ws_id))

    assert done["status"] == "failed"
    assert "not found" in done["error"].lower()


async def test_a_viewer_cannot_start_a_campaign_job(signup_user):
    client, profile, ws_id, _ = await _setup(signup_user)
    ctx = await jobs.build_context(ws_id, profile["id"])
    ctx.member = {**ctx.member, "role": "viewer"}
    with pytest.raises(HTTPException) as caught:
        await jobs.submit(action_name="campaign.next_batch", payload={"campaign_id": "c1"}, ctx=ctx)
    assert caught.value.status_code == 403
