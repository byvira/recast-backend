"""The render worker's side of the backend: the real routes, the real database and the real signing. The worker's own
program is exercised against this in the end-to-end run, not here."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import cloudinary.utils
import pytest

from app.core.config import settings
from app.db.mongo import get_db, media_assets
from app.video import jobs, storage_params

TOKEN = "test-worker-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Contract": "1", "X-Worker-Id": "t1"}


@pytest.fixture(autouse=True)
def _worker_on(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", True)
    monkeypatch.setattr(settings, "VIDEO_WORKER_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "VIDEO_WORKER_TOKEN_NEXT", "")
    monkeypatch.setattr(settings, "CLOUDINARY_CLOUD_NAME", "democloud")
    monkeypatch.setattr(settings, "CLOUDINARY_API_KEY", "1234")
    monkeypatch.setattr(settings, "CLOUDINARY_API_SECRET", "shhh")


@pytest.fixture(autouse=True)
async def _empty_queue():
    await get_db()["video_jobs"].delete_many({})
    yield


def _edit(seconds: int = 10, fps: int = 30, w: int = 1280, h: int = 720) -> dict:
    n = seconds * fps
    return {"version": 1, "fps": {"num": fps, "den": 1}, "width": w, "height": h, "duration_f": n,
            "video": [{"id": "v1", "src": "cam", "at_f": 0, "dur_f": n}], "audio": [], "text": [], "captions": {"enabled": False}}


INPUTS = {"cam": {"url": "https://res.cloudinary.com/democloud/video/upload/v1/a.mp4", "kind": "video"}}


async def _make(ws: str | None = None, **kw) -> dict:
    return await jobs.enqueue(workspace_id=ws or f"ws-{uuid4()}", user_id="u1", inputs=INPUTS, edit=kw.pop("edit", _edit()), **kw)


async def _claim(api_client, **body) -> dict:
    r = await api_client.post("/api/v1/worker/claim", json={"worker_id": "t1", "version": "0.1.1", "capabilities": {}, **body}, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["job"]


# ── switch and token ─────────────────────────────────────────────────────────────

async def test_with_the_switch_off_nothing_is_handed_out(api_client, monkeypatch):
    await _make()
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", False)
    r = await api_client.post("/api/v1/worker/claim", json={}, headers=HEADERS)
    assert r.status_code == 503


async def test_a_wrong_or_missing_token_is_refused_and_the_next_token_works(api_client, monkeypatch):
    assert (await api_client.post("/api/v1/worker/claim", json={})).status_code == 401
    bad = {**HEADERS, "Authorization": "Bearer nope"}
    assert (await api_client.post("/api/v1/worker/claim", json={}, headers=bad)).status_code == 401
    monkeypatch.setattr(settings, "VIDEO_WORKER_TOKEN_NEXT", "rotated")
    rotated = {**HEADERS, "Authorization": "Bearer rotated"}
    assert (await api_client.post("/api/v1/worker/claim", json={}, headers=rotated)).status_code == 200
    assert (await api_client.post("/api/v1/worker/claim", json={}, headers=HEADERS)).status_code == 200


async def test_a_claim_on_an_empty_queue_is_null_and_marks_the_worker_alive(api_client):
    assert await _claim(api_client) is None
    status = await jobs.worker_status()
    assert status["online"] is True and status["version"] == "0.1.1"


# ── making jobs ──────────────────────────────────────────────────────────────────

async def test_a_request_is_checked_before_it_is_queued():
    for edit, words in (
        (_edit(seconds=settings.VIDEO_MAX_OUTPUT_SECONDS + 1), "minutes long"),
        (_edit(w=1281), "even"),
        ({**_edit(), "fps": {"num": 0, "den": 1}}, "frame rate"),
    ):
        with pytest.raises(jobs.JobError) as caught:
            await _make(edit=edit)
        assert words in caught.value.message
    with pytest.raises(jobs.JobError):
        await jobs.enqueue(workspace_id="w", user_id="u", inputs={"cam": {"url": "http://x/a.mp4"}}, edit=_edit())


async def test_the_same_idempotency_key_returns_the_same_job():
    ws = f"ws-{uuid4()}"
    first = await _make(ws, idem_key="k1")
    second = await _make(ws, idem_key="k1")
    assert first["id"] == second["id"]
    assert await get_db()["video_jobs"].count_documents({"workspace_id": ws}) == 1


# ── claiming and leases ──────────────────────────────────────────────────────────

async def test_two_workers_cannot_claim_the_same_job(api_client):
    await _make()
    a, b = await asyncio.gather(_claim(api_client), _claim(api_client))
    assert [x for x in (a, b) if x] and len([x for x in (a, b) if x]) == 1


async def test_a_job_longer_than_the_worker_can_render_is_left_in_the_queue(api_client):
    job = await _make(edit=_edit(seconds=600))
    assert await _claim(api_client, capabilities={"max_output_seconds": 300}) is None
    assert (await jobs.get_job(job["workspace_id"], job["id"]))["status"] == "queued"


async def test_a_workspace_with_nothing_running_is_served_before_one_that_is_already_running(api_client):
    busy, idle = f"ws-{uuid4()}", f"ws-{uuid4()}"
    await _make(busy)
    assert (await _claim(api_client))["id"]            # busy now has one running
    older_busy = await _make(busy)
    newer_idle = await _make(idle)
    got = await _claim(api_client)
    assert got["id"] == newer_idle["id"] and got["id"] != older_busy["id"]


async def test_heartbeat_extends_the_lease_reports_progress_and_carries_a_cancel(api_client):
    job = await _make()
    claimed = await _claim(api_client)
    url = f"/api/v1/worker/jobs/{claimed['id']}/heartbeat"
    r = await api_client.post(url, json={"lease_token": claimed["lease_token"],
                                          "progress": {"stage": "Rendering", "fraction": 0.4, "segments_done": 2, "segments_total": 5}},
                              headers=HEADERS)
    assert r.status_code == 200 and r.json()["cancel"] is False
    shown = await jobs.get_job(job["workspace_id"], job["id"])
    assert shown["progress"]["fraction"] == 0.4 and shown["status"] == "claimed"

    await jobs.cancel_job(job["workspace_id"], job["id"])
    r = await api_client.post(url, json={"lease_token": claimed["lease_token"], "progress": {}}, headers=HEADERS)
    assert r.json()["cancel"] is True


async def test_a_heartbeat_with_the_wrong_lease_is_told_the_lease_is_lost(api_client):
    await _make()
    claimed = await _claim(api_client)
    r = await api_client.post(f"/api/v1/worker/jobs/{claimed['id']}/heartbeat", json={"lease_token": "wrong"}, headers=HEADERS)
    assert r.status_code == 409


async def test_a_queued_job_cancels_at_once_and_a_finished_one_cannot_be_cancelled():
    job = await _make()
    assert (await jobs.cancel_job(job["workspace_id"], job["id"]))["status"] == "cancelled"
    with pytest.raises(jobs.JobError) as caught:
        await jobs.cancel_job(job["workspace_id"], job["id"])
    assert caught.value.status == 409


async def test_a_worker_that_goes_quiet_loses_the_job_and_it_is_failed_after_the_last_attempt(api_client):
    job = await _make()
    for attempt in range(1, settings.VIDEO_WORKER_MAX_ATTEMPTS + 1):
        claimed = await _claim(api_client)
        assert claimed and claimed["id"] == job["id"]
        past = datetime.now(timezone.utc) - timedelta(seconds=5)
        await get_db()["video_jobs"].update_one({"id": job["id"]}, {"$set": {"lease_expires_at": past}})
    assert await _claim(api_client) is None           # reaped: out of attempts
    final = await jobs.get_job(job["workspace_id"], job["id"])
    assert final["status"] == "failed" and final["attempts"] == settings.VIDEO_WORKER_MAX_ATTEMPTS
    # the old worker, if it wakes up, is told the lease is gone
    r = await api_client.post(f"/api/v1/worker/jobs/{job['id']}/heartbeat", json={"lease_token": claimed["lease_token"]}, headers=HEADERS)
    assert r.status_code == 409


# ── failure reports ──────────────────────────────────────────────────────────────

async def _fail(api_client, claimed: dict, **body):
    return await api_client.post(f"/api/v1/worker/jobs/{claimed['id']}/fail",
                                 json={"lease_token": claimed["lease_token"], **body}, headers=HEADERS)


async def test_a_retryable_failure_goes_back_in_the_queue(api_client):
    job = await _make()
    claimed = await _claim(api_client)
    r = await _fail(api_client, claimed, code="network", message="x", retryable=True)
    assert r.json()["requeued"] is True
    assert (await jobs.get_job(job["workspace_id"], job["id"]))["status"] == "queued"


async def test_a_bad_job_is_never_retried_and_shows_the_workers_plain_message(api_client):
    job = await _make()
    claimed = await _claim(api_client)
    r = await _fail(api_client, claimed, code="bad_job", message="The video could not be read.", retryable=True)
    assert r.json()["requeued"] is False
    shown = await jobs.get_job(job["workspace_id"], job["id"])
    assert shown["status"] == "failed" and shown["error"] == "The video could not be read."


async def test_a_restarted_worker_does_not_use_up_an_attempt(api_client):
    job = await _make()
    claimed = await _claim(api_client)
    await _fail(api_client, claimed, code="worker_stopped", message="restart", retryable=True)
    assert (await jobs.get_job(job["workspace_id"], job["id"]))["attempts"] == 0


# ── upload parameters ────────────────────────────────────────────────────────────

async def test_upload_parameters_are_signed_scoped_and_chunked_when_large(api_client):
    job = await _make()
    claimed = await _claim(api_client)
    url = f"/api/v1/worker/jobs/{claimed['id']}/upload-params"
    r = await api_client.post(url, json={"lease_token": claimed["lease_token"], "purpose": "video", "filename": "out.mp4",
                                         "content_type": "video/mp4", "bytes": 5_000_000}, headers=HEADERS)
    assert r.status_code == 200
    p = r.json()
    assert p["url"] == "https://api.cloudinary.com/v1_1/democloud/video/upload" and "chunk_bytes" not in p
    fields = dict(p["fields"])
    signature = fields.pop("signature")
    fields.pop("api_key")
    assert signature == cloudinary.utils.api_sign_request(fields, "shhh")
    assert fields["folder"] == f"recast/video/{job['workspace_id']}" and fields["public_id"].startswith(job["id"])
    assert "shhh" not in str(p)

    big = await api_client.post(url, json={"lease_token": claimed["lease_token"], "purpose": "video", "filename": "out.mp4",
                                           "bytes": 300 * 1024 * 1024}, headers=HEADERS)
    assert big.json()["chunk_bytes"] == storage_params.CHUNK_BYTES

    frame = await api_client.post(url, json={"lease_token": claimed["lease_token"], "purpose": "frame", "filename": "f.jpg", "bytes": 90_000},
                                  headers=HEADERS)
    assert frame.json()["url"].endswith("/image/upload")
    too_big = await api_client.post(url, json={"lease_token": claimed["lease_token"], "purpose": "frame", "filename": "f.jpg",
                                               "bytes": 50 * 1024 * 1024}, headers=HEADERS)
    assert too_big.status_code == 400
    wrong = await api_client.post(url, json={"lease_token": "nope", "purpose": "video", "filename": "o.mp4", "bytes": 10}, headers=HEADERS)
    assert wrong.status_code == 409


# ── delivery checks ──────────────────────────────────────────────────────────────

def _good_report(job: dict, **over) -> dict:
    return {"passed": True, "gate_version": 2, "duration_s": job["duration_s"], "width": 1280, "height": 720, **over}


def _video(url: str = "https://res.cloudinary.com/democloud/video/upload/v1/out.mp4", nbytes: int = 1000) -> dict:
    return {"role": "video", "url": url, "bytes": nbytes, "sha256": "0" * 64}


@pytest.mark.parametrize("label,report_over,video", [
    ("not passed", {"passed": False}, _video()),
    ("old checker", {"gate_version": 1}, _video()),
    ("wrong length", {"duration_s": 99.0}, _video()),
    ("wrong size", {"width": 640}, _video()),
    ("outside our storage", {}, _video(url="https://evil.example.com/out.mp4")),
])
async def test_a_delivery_that_fails_a_check_is_never_saved_or_shown(api_client, label, report_over, video):
    job = await _make()
    claimed = await _claim(api_client)
    r = await api_client.post(f"/api/v1/worker/jobs/{claimed['id']}/complete",
                              json={"lease_token": claimed["lease_token"], "outputs": [video], "report": _good_report(job, **report_over)},
                              headers=HEADERS)
    assert r.status_code == 200 and r.json()["accepted"] is False, label
    shown = await jobs.get_job(job["workspace_id"], job["id"])
    assert shown["status"] == "failed" and shown["error_code"] == "delivery_mismatch" and shown["media_id"] is None
    assert await media_assets.count_documents({"workspace_id": job["workspace_id"], "kind": "video"}) == 0


# ── the owner's kill switch ──────────────────────────────────────────────────────

async def _halt(ws: str, on: bool = True) -> None:
    await get_db()["workspaces"].update_one({"id": ws}, {"$set": {"generation_halted": on}}, upsert=True)


async def test_a_paused_workspace_cannot_queue_a_render_or_a_voice(api_client):
    ws = f"ws-{uuid4()}"
    await _halt(ws)
    try:
        with pytest.raises(jobs.JobError) as err:
            await _make(ws)
        assert err.value.status == 403 and "kill switch" in err.value.message
        with pytest.raises(jobs.JobError):
            await jobs.enqueue_speech(workspace_id=ws, user_id="u1", items=[{"id": "a", "text": "Hello"}], language="en")
    finally:
        await get_db()["workspaces"].delete_one({"id": ws})


async def test_a_render_queued_before_the_pause_is_held_not_lost_and_other_workspaces_carry_on(api_client):
    paused, other = f"ws-{uuid4()}", f"ws-{uuid4()}"
    held = await _make(paused)
    await _halt(paused)
    try:
        assert await _claim(api_client) is None                       # only the paused workspace has work
        mine = await _make(other)
        assert (await _claim(api_client))["id"] == mine["id"]         # another workspace is served
        await _halt(paused, False)
        assert (await _claim(api_client))["id"] == held["id"]         # released: the held render runs
    finally:
        await get_db()["workspaces"].delete_one({"id": paused})
