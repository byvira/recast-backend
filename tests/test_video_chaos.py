"""Bad days for the video pipeline: a delivery that arrives twice, a worker that vanishes after uploading, a receiver that is slow or gone, a
review that is flooded, storage that fails half way. Each must end in a plain state and never leave something half done."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.db.mongo import get_db, media_assets
from app.video import deletion, jobs, webhooks
from tests.conftest import create_workspace
from tests.test_video_worker_routes import HEADERS, _claim, _make, _good_report, _video, _worker_on  # noqa: F401


@pytest.fixture(autouse=True)
async def _clean_queue():
    await get_db()["video_jobs"].delete_many({})
    yield


# ── the worker's side ────────────────────────────────────────────────────────────

async def test_a_finished_render_delivered_twice_makes_one_video_not_two(api_client, monkeypatch):
    async def size(url):
        return 1000

    monkeypatch.setattr(jobs, "_remote_size", size)
    job = await _make()
    claimed = await _claim(api_client)
    body = {"lease_token": claimed["lease_token"], "outputs": [_video()], "report": _good_report(job)}

    first = await api_client.post(f"/api/v1/worker/jobs/{claimed['id']}/complete", json=body, headers=HEADERS)
    second = await api_client.post(f"/api/v1/worker/jobs/{claimed['id']}/complete", json=body, headers=HEADERS)

    assert first.status_code == 200 and first.json()["accepted"] is True
    assert second.status_code in (200, 409) and second.json().get("accepted") is not True
    assert await media_assets.count_documents({"workspace_id": job["workspace_id"], "kind": "video"}) == 1


async def test_a_worker_that_stored_the_file_but_never_reported_leaves_the_job_to_be_retried_not_half_finished(api_client):
    job = await _make()
    claimed = await _claim(api_client)
    await get_db()["video_jobs"].update_one({"id": claimed["id"]}, {"$set": {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=5)}})

    again = await _claim(api_client)                      # the lease ran out: the job goes to the next worker call

    assert again is not None and again["id"] == job["id"]
    assert again["lease_token"] != claimed["lease_token"]
    stale = await api_client.post(f"/api/v1/worker/jobs/{claimed['id']}/complete",
                                  json={"lease_token": claimed["lease_token"], "outputs": [_video()], "report": _good_report(job)}, headers=HEADERS)
    assert stale.status_code == 409                       # the first worker's late answer is refused
    assert await media_assets.count_documents({"workspace_id": job["workspace_id"], "kind": "video"}) == 0


async def test_storage_that_cannot_report_the_size_means_the_delivery_is_refused_and_nothing_is_shown(api_client, monkeypatch):
    async def unreadable(url):
        return None

    monkeypatch.setattr(jobs, "_remote_size", unreadable)
    job = await _make()
    claimed = await _claim(api_client)

    r = await api_client.post(f"/api/v1/worker/jobs/{claimed['id']}/complete",
                              json={"lease_token": claimed["lease_token"], "outputs": [_video()], "report": _good_report(job)}, headers=HEADERS)

    assert r.status_code == 200 and r.json()["accepted"] is False
    assert (await jobs.get_job(job["workspace_id"], job["id"]))["status"] == "failed"
    assert await media_assets.count_documents({"workspace_id": job["workspace_id"], "kind": "video"}) == 0


# ── webhooks ─────────────────────────────────────────────────────────────────────

class _Gone:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        raise self.error


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), httpx.RemoteProtocolError("cut off")])
async def test_a_receiver_that_is_slow_or_gone_is_retried_later_with_a_plain_reason(signup_user, monkeypatch, error):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Chaos {uuid4().hex[:6]}")

    async def fine(url):
        return None

    monkeypatch.setattr(webhooks, "assert_safe_url", fine)
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda *a, **kw: _Gone(error))
    await client.post("/api/v1/developer/webhooks", headers={"X-Workspace-Id": ws}, json={"url": "https://example.com/h", "events": ["video.render_done"]})

    await webhooks.dispatch(ws, "video.render_done", {"project_id": "p"})
    await asyncio.sleep(0.3)

    d = await get_db()["video_webhook_deliveries"].find_one({"workspace_id": ws})
    assert d["status"] == "retrying" and d["attempts"] == 1 and "Could not reach the receiver" in d["last_error"] and d["next_at"] is not None


async def test_a_webhook_removed_between_queueing_and_retry_is_not_sent_to_and_the_delivery_is_closed(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Chaos {uuid4().hex[:6]}")
    await get_db()["video_webhook_deliveries"].insert_one({
        "id": uuid4().hex, "workspace_id": ws, "hook_id": "removed-hook", "event": "video.render_done", "data": {}, "status": "retrying", "attempts": 1,
        "created_at": datetime.now(timezone.utc), "next_at": datetime.now(timezone.utc) - timedelta(seconds=1)})

    sent = await webhooks.retry_due()

    d = await get_db()["video_webhook_deliveries"].find_one({"workspace_id": ws})
    assert sent == 0 and d["status"] == "failed" and "removed" in d["last_error"]


async def test_a_webhook_that_blows_up_inside_never_breaks_the_work_that_caused_it(monkeypatch):
    from app.video import activity

    async def explode(*a, **kw):
        raise RuntimeError("receiver store is down")

    monkeypatch.setattr(webhooks, "dispatch", explode)
    ws = f"ws-{uuid4()}"
    await get_db()["video_projects"].insert_one({"id": "p1", "workspace_id": ws, "title": "T"})

    await activity.video_made(ws, None, "p1", "job-chaos", duration_s=5.0)             # must return normally
    await activity.review_answered(ws, None, "p1", "l1", "approved", "Sam", "")


# ── review flood and deletion ────────────────────────────────────────────────────

async def test_a_review_that_is_flooded_with_comments_stops_at_its_limit_with_a_plain_message(signup_user):
    from app.api.v1 import video_review

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Chaos {uuid4().hex[:6]}")
    pid, mid, jid = uuid4().hex, uuid4().hex, uuid4().hex
    await get_db()["video_projects"].insert_one({"id": pid, "workspace_id": ws, "title": "T", "edit": {}})
    await media_assets.insert_one({"id": mid, "workspace_id": ws, "kind": "video", "url": "https://cdn.example/a.mp4", "mime_type": "video/mp4",
                                   "created_by": "u1", "created_at": datetime.now(timezone.utc)})
    await get_db()["video_jobs"].insert_one({"id": jid, "workspace_id": ws, "project_id": pid, "status": "done", "media_id": mid, "finished_at": datetime.now(timezone.utc)})
    made = await client.post(f"/api/v1/video-review/projects/{pid}/links", headers={"X-Workspace-Id": ws}, json={})
    token, link_id = made.json()["url"].rsplit("/", 1)[1], made.json()["id"]
    await get_db()["video_review_comments"].insert_many([{"id": f"c{i}", "link_id": link_id, "workspace_id": ws, "time_ms": i, "text": "x", "author_name": "A"}
                                                         for i in range(video_review.MAX_COMMENTS_PER_LINK)])

    full = await client.post(f"/api/v1/video-review-public/{token}/comments", json={"time_ms": 1, "text": "one more", "guest_name": "Sam"})

    assert full.status_code == 429 and "as many comments as it can hold" in full.json()["detail"]


async def test_a_deletion_where_storage_fails_for_every_file_still_removes_the_records_and_keeps_every_file_on_the_retry_list(monkeypatch):
    async def always_fails(url):
        return "error"

    monkeypatch.setattr(deletion, "remove_file", always_fails)
    ws, mid = f"ws-{uuid4()}", uuid4().hex
    await media_assets.insert_one({
        "id": mid, "workspace_id": ws, "kind": "video", "url": f"https://res.cloudinary.com/x/video/upload/v1/{mid}.mp4",
        "poster_url": f"https://res.cloudinary.com/x/image/upload/v1/{mid}.jpg", "created_by": "u1", "created_at": datetime.now(timezone.utc)})

    out = await deletion.delete_media(ws, mid, actor="u1")

    assert out["files_removed"] == 0 and out["files_to_retry"] == 2
    assert await media_assets.count_documents({"id": mid}) == 0
    assert await get_db()["video_file_cleanup"].count_documents({"workspace_id": ws}) == 2


# ── a render that ended without a video can be asked for again ───────────────────

async def test_asking_again_after_a_failed_render_makes_a_new_job_not_the_dead_one():
    ws = f"ws-{uuid4()}"
    first = await _make(ws, idem_key="proj:p1:v1:standard:-14.0:h264")
    await get_db()["video_jobs"].update_one({"id": first["id"]}, {"$set": {"status": "failed", "error_code": "worker_lost"}})

    second = await _make(ws, idem_key="proj:p1:v1:standard:-14.0:h264")

    assert second["id"] != first["id"] and second["status"] == "queued"


async def test_asking_again_while_a_render_is_waiting_returns_the_same_job():
    ws = f"ws-{uuid4()}"
    first = await _make(ws, idem_key="proj:p2:v1:standard:-14.0:h264")
    again = await _make(ws, idem_key="proj:p2:v1:standard:-14.0:h264")
    assert again["id"] == first["id"]


async def test_a_delivery_that_arrives_after_the_job_was_taken_away_keeps_no_video(api_client, monkeypatch):
    async def size(url):
        # while the stored file is being checked, the member cancels and the job is closed
        await get_db()["video_jobs"].update_one({"id": claimed["id"]}, {"$set": {"status": "cancelled", "lease_token": None}})
        return 1000

    monkeypatch.setattr(jobs, "_remote_size", size)
    job = await _make()
    claimed = await _claim(api_client)
    body = {"lease_token": claimed["lease_token"], "outputs": [_video()], "report": _good_report(job)}

    r = await api_client.post(f"/api/v1/worker/jobs/{claimed['id']}/complete", json=body, headers=HEADERS)

    assert r.status_code == 409
    assert await media_assets.count_documents({"workspace_id": job["workspace_id"], "kind": "video"}) == 0
