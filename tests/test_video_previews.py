"""Preview jobs (small copy, poster, thumbnails, waveform) on the backend, and daily retention, on the real database."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core.config import settings
from app.db.mongo import get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import jobs, retention, storage_params


@pytest.fixture(autouse=True)
def _cloud(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDINARY_CLOUD_NAME", "democloud")
    monkeypatch.setattr(settings, "CLOUDINARY_API_KEY", "1234")
    monkeypatch.setattr(settings, "CLOUDINARY_API_SECRET", "shhh")
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", True)


@pytest.fixture(autouse=True)
async def _empty_queue():
    await get_db()["video_jobs"].delete_many({})
    yield


async def _item(ws: str, kind: str = "video") -> dict:
    row = MediaAsset(
        id=str(uuid4()), workspace_id=ws, kind=MediaKind(kind), url=f"https://res.cloudinary.com/democloud/{kind}/upload/v1/{uuid4()}.mp4",
        mime_type="video/mp4", duration_s=12.0, source=MediaSource.UPLOADED, created_by="u1", created_at=datetime.now(timezone.utc),
    ).model_dump()
    await media_assets.insert_one(dict(row))
    return row


def _url(role: str, ws: str) -> str:
    kind = {"proxy": "video", "poster": "image", "strip": "image", "waveform": "raw"}[role]
    return f"https://res.cloudinary.com/democloud/{kind}/upload/v1/recast/video_preview/{ws}/{uuid4()}_{role}"


async def _claim_one() -> dict:
    claimed = await jobs.claim("w1", "0.2.0", {})
    assert claimed and claimed["kind"] == "proxy"
    return claimed


async def test_a_preview_job_is_queued_once_and_handed_to_the_worker_as_a_proxy_job():
    ws = f"ws-{uuid4()}"
    item = await _item(ws)
    first = await jobs.enqueue_preview(workspace_id=ws, user_id="u1", media_id=item["id"], url=item["url"], kind="video", spec={"max_height": 360})
    again = await jobs.enqueue_preview(workspace_id=ws, user_id="u1", media_id=item["id"], url=item["url"], kind="video")
    assert first["id"] == again["id"] and first["kind"] == "proxy"
    claimed = await _claim_one()
    assert claimed["inputs"]["src"]["url"] == item["url"] and claimed["proxy"] == {"max_height": 360} and "edit" not in claimed


async def test_each_preview_file_gets_its_own_signed_and_scoped_upload():
    ws = f"ws-{uuid4()}"
    for role, resource in (("proxy", "video"), ("poster", "image"), ("strip", "image"), ("waveform", "raw")):
        params = storage_params.upload_params(workspace_id=ws, job_id="j1", purpose=role, filename=f"{role}.bin", nbytes=1000)
        assert params["url"].endswith(f"/democloud/{resource}/upload")
        assert params["fields"]["folder"] == f"recast/video_preview/{ws}" and "shhh" not in str(params)
    with pytest.raises(ValueError):
        storage_params.upload_params(workspace_id=ws, job_id="j", purpose="secrets", filename="x", nbytes=10)
    with pytest.raises(ValueError):
        storage_params.upload_params(workspace_id=ws, job_id="j", purpose="waveform", filename="x", nbytes=50 * 1024 * 1024)


async def test_a_report_that_is_not_a_preview_report_is_refused_and_the_item_says_so():
    ws = f"ws-{uuid4()}"
    item = await _item(ws)
    await jobs.enqueue_preview(workspace_id=ws, user_id="u1", media_id=item["id"], url=item["url"], kind="video")
    claimed = await _claim_one()
    out = await jobs.complete(claimed["id"], claimed["lease_token"], [{"role": "proxy", "url": _url("proxy", ws), "bytes": 10}],
                              {"passed": True, "kind": "render"})
    assert out["accepted"] is False
    row = await media_assets.find_one({"id": item["id"]})
    assert row["preview_status"] == "failed" and "try again" in row["preview_error"]


async def test_a_preview_file_outside_our_storage_is_refused():
    ws = f"ws-{uuid4()}"
    item = await _item(ws)
    await jobs.enqueue_preview(workspace_id=ws, user_id="u1", media_id=item["id"], url=item["url"], kind="video")
    claimed = await _claim_one()
    out = await jobs.complete(
        claimed["id"], claimed["lease_token"],
        [{"role": "proxy", "url": "https://evil.example.com/p.mp4", "bytes": 10}, {"role": "poster", "url": _url("poster", ws), "bytes": 5}],
        {"passed": True, "kind": "proxy"},
    )
    assert out["accepted"] is False
    assert (await media_assets.find_one({"id": item["id"]}))["preview_status"] == "failed"


async def test_a_failed_preview_job_is_recorded_on_the_item_for_the_screen():
    ws = f"ws-{uuid4()}"
    item = await _item(ws)
    await jobs.enqueue_preview(workspace_id=ws, user_id="u1", media_id=item["id"], url=item["url"], kind="video")
    claimed = await _claim_one()
    await jobs.fail(claimed["id"], claimed["lease_token"], "bad_input", "This video could not be read.", False)
    row = await media_assets.find_one({"id": item["id"]})
    assert row["preview_status"] == "failed" and row["preview_error"] == "This video could not be read."


async def test_retention_removes_only_unfinished_uploads_and_old_records_never_a_members_media():
    ws = f"ws-{uuid4()}"
    old = datetime.now(timezone.utc) - timedelta(days=5)
    db = get_db()
    await db["video_uploads"].insert_many([
        {"id": "u-stale", "workspace_id": ws, "status": "started", "created_at": old},
        {"id": "u-done", "workspace_id": ws, "status": "done", "created_at": old},
        {"id": "u-new", "workspace_id": ws, "status": "started", "created_at": datetime.now(timezone.utc)},
    ])
    kept_media = await _item(ws)
    await db["video_jobs"].insert_many([
        {"id": "j-old", "workspace_id": ws, "status": "done", "finished_at": datetime.now(timezone.utc) - timedelta(days=120), "kind": "render"},
        {"id": "j-new", "workspace_id": ws, "status": "done", "finished_at": datetime.now(timezone.utc), "kind": "render"},
        {"id": "j-wait", "workspace_id": ws, "status": "queued", "created_at": old, "kind": "render"},
    ])
    await db["agent_decisions"].insert_many([
        {"id": "d-old", "workspace_id": ws, "created_at": datetime.now(timezone.utc) - timedelta(days=400), "outcome": "ok"},
        {"id": "d-open", "workspace_id": ws, "created_at": datetime.now(timezone.utc) - timedelta(days=400), "outcome": "needs_decision"},
    ])
    removed_uploads = await retention.sweep_unfinished_uploads()
    records = await retention.trim_records()
    assert removed_uploads >= 1
    assert await db["video_uploads"].find_one({"id": "u-stale"}) is None
    assert await db["video_uploads"].find_one({"id": "u-done"}) is not None and await db["video_uploads"].find_one({"id": "u-new"}) is not None
    assert await db["video_jobs"].find_one({"id": "j-old"}) is None
    assert await db["video_jobs"].find_one({"id": "j-new"}) is not None and await db["video_jobs"].find_one({"id": "j-wait"}) is not None
    assert await db["agent_decisions"].find_one({"id": "d-old"}) is None
    assert await db["agent_decisions"].find_one({"id": "d-open"}) is not None      # a decision still waiting for a person stays
    assert await media_assets.find_one({"id": kept_media["id"]}) is not None
    assert records["job_records"] >= 1


def test_retention_is_registered_as_a_daily_job():
    from app.workers.jobs import JOBS

    job = next(j for j in JOBS if j.name == "video_retention")
    assert job.hour == frozenset({3}) and job.minute == frozenset({41})
