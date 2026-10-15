"""Keep the speaker in frame: the job a member starts, the result the worker reports (checked before it is kept), what the member reads back,
and the edit list accepting the path. Real database; only the worker is stood in for."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.config import settings
from app.db.mongo import get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import edl, jobs
from tests.conftest import create_workspace


@pytest.fixture(autouse=True)
def _cloud(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDINARY_CLOUD_NAME", "democloud")
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", True)


@pytest.fixture(autouse=True)
async def _clean():
    await get_db()["video_jobs"].delete_many({})
    await get_db()["video_reframe_paths"].delete_many({})
    yield


async def _video(ws: str, *, preview: bool = True) -> dict:
    row = MediaAsset(
        id=str(uuid4()), workspace_id=ws, kind=MediaKind.VIDEO, url=f"https://res.cloudinary.com/democloud/video/upload/v1/{uuid4()}.mp4",
        mime_type="video/mp4", duration_s=60.0, source=MediaSource.UPLOADED, created_by="u1", created_at=datetime.now(timezone.utc),
    ).model_dump()
    if preview:
        row["preview_status"] = "ready"
        row["preview"] = {"proxy_url": f"https://res.cloudinary.com/democloud/video/upload/v1/recast/video_preview/{ws}/{uuid4()}_proxy", "meta": {"duration_s": 60.0}}
    await media_assets.insert_one(dict(row))
    return row


def _report(**over):
    base = {"kind": "reframe", "passed": True, "faces_checked": True, "looked": 50, "with_face": 48, "window": {"w": 0.3164, "h": 1.0},
            "on_camera": {"face_share": 0.96, "centred_share": 0.8},
            "path": [{"t": 0.0, "x": 0.4, "y": 0.5}, {"t": 10.0, "x": 0.7, "y": 0.5}]}
    base.update(over)
    return base


async def _claimed(ws, media, **spec):
    await jobs.enqueue_reframe(workspace_id=ws, user_id="u1", media_id=media["id"], url=media["preview"]["proxy_url"],
                               aspect=spec.get("aspect", 0.5625), start_s=spec.get("start_s", 0), end_s=spec.get("end_s", 30))
    return await jobs.claim("w1", "0.10.0", {})


async def test_a_reframe_job_is_queued_once_handed_over_with_its_part_and_replaced_after_a_failure():
    ws = f"ws-{uuid4()}"
    media = await _video(ws)
    url = media["preview"]["proxy_url"]
    first = await jobs.enqueue_reframe(workspace_id=ws, user_id="u1", media_id=media["id"], url=url, aspect=0.5625, start_s=2, end_s=30)
    again = await jobs.enqueue_reframe(workspace_id=ws, user_id="u1", media_id=media["id"], url=url, aspect=0.5625, start_s=2, end_s=30)
    assert first["id"] == again["id"]
    claimed = await jobs.claim("w1", "0.10.0", {})
    assert claimed["kind"] == "reframe" and claimed["reframe"] == {"aspect": 0.5625, "start_s": 2.0, "end_s": 30.0, "step_s": 0.4}
    await jobs.fail(claimed["id"], claimed["lease_token"], "render_failed", "no", False)
    third = await jobs.enqueue_reframe(workspace_id=ws, user_id="u1", media_id=media["id"], url=url, aspect=0.5625, start_s=2, end_s=30)
    assert third["id"] != first["id"]
    for bad in (dict(url="http://insecure/x.mp4"), dict(aspect=9), dict(start_s=30, end_s=30), dict(end_s=5000), dict(step_s=0.01)):
        args = dict(workspace_id=ws, user_id="u1", media_id=media["id"], url=url, aspect=0.5625, start_s=2, end_s=30) | bad
        with pytest.raises(jobs.JobError):
            await jobs.enqueue_reframe(**args)


async def test_the_path_the_worker_reports_is_kept_and_a_bad_one_is_refused():
    ws = f"ws-{uuid4()}"
    media = await _video(ws)
    claimed = await _claimed(ws, media)
    assert (await jobs.complete(claimed["id"], claimed["lease_token"], [], _report()))["accepted"] is True
    doc = await get_db()["video_reframe_paths"].find_one({"workspace_id": ws, "media_id": media["id"]})
    assert doc["path"][1] == {"t": 10.0, "x": 0.7, "y": 0.5} and doc["faces_checked"] is True and doc["window"] == {"w": 0.3164, "h": 1.0}
    assert doc["on_camera"] == {"face_share": 0.96, "centred_share": 0.8}
    bad_reports = [
        _report(path=[{"t": 0, "x": 1.4, "y": 0.5}]), _report(path=[{"t": 5, "x": 0.4, "y": 0.5}, {"t": 5, "x": 0.5, "y": 0.5}]),
        _report(path=[{"t": 0, "x": "0.4", "y": 0.5}]), _report(path="no"), _report(window={"w": 0, "h": 1}), _report(passed=False), _report(kind="frames"),
        _report(path=[{"t": 0, "x": 0.5, "y": 0.5}] * 3), _report(path=[{"t": float(i), "x": 0.5, "y": 0.5} for i in range(241)]),
    ]
    for report in bad_reports:
        await get_db()["video_jobs"].delete_many({"workspace_id": ws})
        c = await _claimed(ws, media)
        assert (await jobs.complete(c["id"], c["lease_token"], [], report))["accepted"] is False, report


async def test_a_machine_with_no_face_finder_gives_an_empty_path_and_says_so():
    ws = f"ws-{uuid4()}"
    media = await _video(ws)
    claimed = await _claimed(ws, media)
    assert (await jobs.complete(claimed["id"], claimed["lease_token"], [], _report(faces_checked=False, looked=0, with_face=0, path=[])))["accepted"]
    doc = await get_db()["video_reframe_paths"].find_one({"workspace_id": ws})
    assert doc["path"] == [] and doc["faces_checked"] is False


async def test_the_member_starts_the_pass_and_reads_back_running_then_done_with_a_plain_note(signup_user, monkeypatch):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Reframe")
    head = {"X-Workspace-Id": ws}
    media = await _video(ws)
    q = {"media_id": media["id"], "aspect": 0.5625, "start_s": 0, "end_s": 30}
    assert (await client.get("/api/v1/video-reframe", params=q, headers=head)).json()["status"] == "none"
    started = await client.post("/api/v1/video-reframe", json=q, headers=head)
    assert started.status_code == 201, started.text
    assert (await client.get("/api/v1/video-reframe", params=q, headers=head)).json()["status"] == "queued"
    claimed = await jobs.claim("w1", "0.10.0", {})
    assert (await client.get("/api/v1/video-reframe", params=q, headers=head)).json()["status"] == "running"
    await jobs.complete(claimed["id"], claimed["lease_token"], [], _report(faces_checked=False, looked=0, with_face=0, path=[]))
    done = (await client.get("/api/v1/video-reframe", params=q, headers=head)).json()
    assert done["status"] == "done" and done["path"] == [] and "face finder" in done["note"]


async def test_a_part_past_the_end_is_kept_inside_the_video_and_another_workspace_sees_nothing(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Reframe")
    other_client, _ = await signup_user()
    other = await create_workspace(other_client, "Other")
    media = await _video(ws)
    q = {"media_id": media["id"], "aspect": 0.5625, "start_s": 0, "end_s": 9999}
    assert (await client.post("/api/v1/video-reframe", json=q, headers={"X-Workspace-Id": ws})).json()["end_s"] == 60.0
    assert (await client.post("/api/v1/video-reframe", json={**q, "start_s": 70}, headers={"X-Workspace-Id": ws})).status_code == 400
    assert (await other_client.post("/api/v1/video-reframe", json=q, headers={"X-Workspace-Id": other})).status_code == 404
    assert (await client.post("/api/v1/video-reframe", json={**q, "media_id": "nope"}, headers={"X-Workspace-Id": ws})).status_code == 404
    unready = await _video(ws, preview=False)
    assert (await client.post("/api/v1/video-reframe", json={**q, "media_id": unready["id"]}, headers={"X-Workspace-Id": ws})).status_code == 409


def _edit(clip):
    return {"fps": {"num": 30, "den": 1}, "width": 1080, "height": 1920, "duration_f": 90, "video": [clip], "audio": [], "text": []}


def _clip(**extra):
    return {"id": "c1", "media_id": "m1", "at_f": 0, "dur_f": 90, "crop": {"x": 0, "y": 0, "w": 0.3164, "h": 1.0}, **extra}


def test_the_edit_list_keeps_a_follow_path_and_refuses_a_bad_one():
    good = edl.validate(_edit(_clip(follow=[{"t_f": 0, "x": 0.4, "y": 0.5}, {"t_f": 60, "x": 0.7, "y": 0.5}])))
    assert good["video"][0]["follow"] == [{"t_f": 0, "x": 0.4, "y": 0.5}, {"t_f": 60, "x": 0.7, "y": 0.5}]
    assert edl.validate(_edit(_clip()))["video"][0]["follow"] == []
    for bad in ([{"t_f": 5, "x": 0.4, "y": 0.5}, {"t_f": 5, "x": 0.5, "y": 0.5}], [{"t_f": 0, "x": 2, "y": 0.5}], "x",
                [{"t_f": i, "x": 0.5, "y": 0.5} for i in range(241)]):
        with pytest.raises(Exception):
            edl.validate(_edit(_clip(follow=bad)))
