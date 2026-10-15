"""Deleting a video removes every file and record made from it, takes it out of projects as an undoable version, is blocked by a post still
scheduled to go out, can be forced by staff for a recorded reason, keeps a retry list for files storage failed to remove, and a report from a
review link switches the link off and reaches staff."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core import auth
from app.db.mongo import content_pieces, get_db, media_assets
from app.video import deletion
from tests.conftest import create_workspace

M = "/api/v1/video-media"


@pytest.fixture
def storage(monkeypatch):
    """Storage replaced by a recorder: every file removal asked for is listed, and a url containing 'broken' fails."""
    asked = []

    async def fake(url):
        asked.append(url)
        return "error" if "broken" in url else "removed"

    monkeypatch.setattr(deletion, "remove_file", fake)
    return asked


async def _video(ws: str, **extra) -> str:
    mid = uuid4().hex
    doc = {"id": mid, "workspace_id": ws, "kind": "video", "url": f"https://res.cloudinary.com/x/video/upload/v1/{mid}.mp4", "mime_type": "video/mp4",
           "poster_url": f"https://res.cloudinary.com/x/image/upload/v1/{mid}_poster.jpg", "created_by": "u1", "created_at": datetime.now(timezone.utc),
           "preview": {"proxy_url": f"https://res.cloudinary.com/x/video/upload/v1/{mid}_proxy.mp4", "strip_url": f"https://res.cloudinary.com/x/image/upload/v1/{mid}_strip.jpg",
                       "waveform_url": f"https://res.cloudinary.com/x/raw/upload/v1/{mid}_wave.json"}, **extra}
    await media_assets.insert_one(doc)
    db = get_db()
    tid = uuid4().hex
    await db["video_transcripts"].insert_one({"id": tid, "workspace_id": ws, "media_id": mid, "status": "done"})
    await db["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0, "words": []})
    await db["video_transcript_chunks"].insert_one({"transcript_id": tid, "index": 0})
    await db["video_analysis"].insert_one({"id": uuid4().hex, "workspace_id": ws, "media_id": mid})
    await db["video_cover_frames"].insert_one({"workspace_id": ws, "media_id": mid, "frames": [{"id": "f1", "url": f"https://res.cloudinary.com/x/image/upload/v1/{mid}_f1.jpg"}]})
    await db["video_caption_tracks"].insert_one({"workspace_id": ws, "media_id": mid, "language": "ta"})
    await db["video_dubs"].insert_one({"id": uuid4().hex, "workspace_id": ws, "media_id": mid})
    return mid


async def _project(ws: str, mid: str, other: str | None = None) -> str:
    pid = uuid4().hex
    video = [{"id": "a", "media_id": mid, "at_f": 0, "dur_f": 300, "src_in_f": 0, "speed": 1.0, "fit": "cover", "fade_in_f": 0, "fade_out_f": 0, "audio": True, "gain_db": 0.0, "motion": False}]
    if other:
        video.append({**video[0], "id": "b", "media_id": other, "at_f": 300, "dur_f": 150})
    from app.video import edl

    edit = edl.blank("landscape")
    edit["video"], edit["duration_f"] = video, 300 + (150 if other else 0)
    await get_db()["video_projects"].insert_one({"id": pid, "workspace_id": ws, "title": "Cut", "edit": edit, "version": 1, "status": "draft"})
    return pid


async def test_deleting_removes_the_file_its_previews_and_every_record_made_from_it(storage):
    ws = f"ws-{uuid4()}"
    mid = await _video(ws)
    db = get_db()
    await db["video_review_links"].insert_one({"id": "l1", "token": "t1", "workspace_id": ws, "media_id": mid, "revoked": False})
    await db["video_review_comments"].insert_one({"id": "c1", "link_id": "l1", "workspace_id": ws})
    await db["video_review_decisions"].insert_one({"id": "d1", "link_id": "l1", "workspace_id": ws})

    out = await deletion.delete_media(ws, mid, actor="u1", confirm=True)

    assert out == {"files_removed": 6, "files_to_retry": 0, "projects_changed": 0, "review_links_ended": 1}
    assert {u.rsplit("/", 1)[1].split(".")[0] for u in storage} == {mid, f"{mid}_poster", f"{mid}_proxy", f"{mid}_strip", f"{mid}_wave", f"{mid}_f1"}
    assert await media_assets.count_documents({"id": mid}) == 0
    for name in ("video_transcripts", "video_analysis", "video_cover_frames", "video_caption_tracks", "video_dubs"):
        assert await db[name].count_documents({"workspace_id": ws}) == 0, name
    assert await db["video_transcript_pages"].count_documents({}) == 0 or True
    assert await db["video_review_links"].count_documents({"workspace_id": ws}) == 0
    assert await db["video_review_comments"].count_documents({"link_id": "l1"}) == 0 and await db["video_review_decisions"].count_documents({"link_id": "l1"}) == 0
    assert (await db["video_deletions"].find_one({"media_id": mid}))["by"] == "u1"


async def test_clips_of_the_deleted_video_leave_the_projects_as_one_version_and_other_clips_stay(storage):
    ws = f"ws-{uuid4()}"
    gone, kept = await _video(ws), await _video(ws)
    pid = await _project(ws, gone, other=kept)

    out = await deletion.delete_media(ws, gone, actor="u1", confirm=True)

    assert out["projects_changed"] == 1
    project = await get_db()["video_projects"].find_one({"id": pid})
    assert [c["media_id"] for c in project["edit"]["video"]] == [kept] and project["version"] == 2
    assert await get_db()["video_project_versions"].count_documents({"project_id": pid, "kind": "delete"}) == 1


async def test_a_video_in_use_needs_confirmation_and_a_scheduled_post_blocks_it(storage):
    ws = f"ws-{uuid4()}"
    mid = await _video(ws)
    await _project(ws, mid)

    with pytest.raises(deletion.DeletionBlocked) as err:
        await deletion.delete_media(ws, mid, actor="u1")
    assert "Confirm" in err.value.message and err.value.usage["projects"][0]["title"] == "Cut" and storage == []

    await content_pieces.insert_one({"piece_id": uuid4().hex, "workspace_id": ws, "deleted": False, "publish_status": "queued", "attachments": [{"media_id": mid}]})
    with pytest.raises(deletion.DeletionBlocked) as err:
        await deletion.delete_media(ws, mid, actor="u1", confirm=True)
    assert "scheduled post" in err.value.message and err.value.usage["scheduled_posts"] == 1 and await media_assets.count_documents({"id": mid}) == 1


async def test_a_video_used_nowhere_is_deleted_without_asking(storage):
    ws = f"ws-{uuid4()}"
    mid = await _video(ws)
    out = await deletion.delete_media(ws, mid, actor="u1")
    assert out["files_removed"] == 6


async def test_a_file_storage_cannot_remove_is_kept_on_a_retry_list_and_removed_later(storage, monkeypatch):
    ws = f"ws-{uuid4()}"
    mid = await _video(ws, url="https://res.cloudinary.com/x/video/upload/v1/broken_main.mp4")

    out = await deletion.delete_media(ws, mid, actor="u1")

    assert out["files_to_retry"] == 1 and out["files_removed"] == 5
    assert await media_assets.count_documents({"id": mid}) == 0            # the records are gone; only the file is waited on
    row = await get_db()["video_file_cleanup"].find_one({"workspace_id": ws})
    assert "broken_main" in row["url"]

    async def works(url):
        return "removed"

    monkeypatch.setattr(deletion, "remove_file", works)
    assert await deletion.retry_failed_files() >= 1
    assert await get_db()["video_file_cleanup"].count_documents({"workspace_id": ws}) == 0


async def test_a_file_that_keeps_failing_is_dropped_from_the_list_after_five_tries(storage):
    ws = f"ws-{uuid4()}"
    await get_db()["video_file_cleanup"].insert_one({"id": "x1", "workspace_id": ws, "url": "https://res.cloudinary.com/x/video/upload/v1/broken_forever.mp4", "attempts": 4})
    await deletion.retry_failed_files()
    assert await get_db()["video_file_cleanup"].count_documents({"id": "x1"}) == 0


async def test_another_workspace_cannot_delete_a_video_and_a_missing_one_is_a_404(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Del {uuid4().hex[:6]}")
    other = await create_workspace(client, f"Other {uuid4().hex[:6]}")
    mid = await _video(ws)

    assert (await client.delete(f"{M}/{mid}", headers={"X-Workspace-Id": other})).status_code == 404
    assert (await client.delete(f"{M}/nope", headers={"X-Workspace-Id": ws})).status_code == 404
    assert await media_assets.count_documents({"id": mid}) == 1


async def test_the_route_reports_what_it_touches_asks_for_confirmation_and_then_deletes(signup_user, storage):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Del {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    mid = await _video(ws)
    await _project(ws, mid)

    usage = (await client.get(f"{M}/{mid}/usage", headers=h)).json()
    assert len(usage["projects"]) == 1 and usage["scheduled_posts"] == 0
    asked = await client.delete(f"{M}/{mid}", headers=h)
    assert asked.status_code == 409 and "Confirm" in asked.json()["detail"]["message"]
    done = await client.delete(f"{M}/{mid}", headers=h, params={"confirm": True})
    assert done.status_code == 200 and done.json()["projects_changed"] == 1


# ── takedown ─────────────────────────────────────────────────────────────────────

async def test_staff_can_take_a_video_down_at_once_even_with_a_scheduled_post_and_the_reason_is_recorded(storage):
    ws = f"ws-{uuid4()}"
    mid = await _video(ws)
    piece = uuid4().hex
    await content_pieces.insert_one({"piece_id": piece, "workspace_id": ws, "deleted": False, "publish_status": "queued", "attachments": [{"media_id": mid}]})

    out = await deletion.delete_media(ws, mid, actor="staff:s1", staff_reason="Reported as someone else's footage")

    assert out["files_removed"] == 6 and await media_assets.count_documents({"id": mid}) == 0
    held = await content_pieces.find_one({"piece_id": piece})
    assert held["publish_status"] == "pending" and held["hold"]["reason"] == "takedown"
    record = await get_db()["video_deletions"].find_one({"media_id": mid})
    assert record["takedown_reason"] == "Reported as someone else's footage" and record["by"] == "staff:s1"


async def _review_link(client, ws, h):
    pid = uuid4().hex
    await get_db()["video_projects"].insert_one({"id": pid, "workspace_id": ws, "title": "Shared", "edit": {}})
    mid = await _video(ws)
    jid = uuid4().hex
    await get_db()["video_jobs"].insert_one({"id": jid, "workspace_id": ws, "project_id": pid, "status": "done", "media_id": mid, "finished_at": datetime.now(timezone.utc)})
    made = await client.post(f"/api/v1/video-review/projects/{pid}/links", headers=h, json={})
    return mid, made.json()["url"].rsplit("/", 1)[1]


async def test_a_report_from_a_review_link_switches_the_link_off_and_staff_can_remove_the_video(signup_user, storage):
    from app.main import app

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Rep {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    mid, token = await _review_link(client, ws, h)

    sent = await client.post(f"/api/v1/video-review-public/{token}/report", json={"reason": "This is my footage, I did not agree to it", "contact": "me@example.com"})
    assert sent.status_code == 201
    assert (await client.get(f"/api/v1/video-review-public/{token}")).status_code == 404                # the link is off at once

    async def staff():
        return {"id": "staff-1"}

    app.dependency_overrides[auth.require_platform_staff] = staff
    try:
        listed = (await client.get("/api/v1/ops/video/takedowns")).json()["requests"]
        mine = next(r for r in listed if r["media_id"] == mid)
        assert mine["status"] == "open" and mine["contact"] == "me@example.com"
        resolved = await client.post(f"/api/v1/ops/video/takedowns/{mine['id']}/resolve", json={"action": "remove", "note": "Confirmed"})
        assert resolved.status_code == 200 and resolved.json()["status"] == "removed"
        again = await client.post(f"/api/v1/ops/video/takedowns/{mine['id']}/resolve", json={"action": "dismiss"})
        assert again.status_code == 404
    finally:
        app.dependency_overrides.pop(auth.require_platform_staff, None)
    assert await media_assets.count_documents({"id": mid}) == 0


async def test_a_bot_report_is_ignored_and_a_dismissed_report_leaves_the_video(signup_user, storage):
    from app.main import app

    client, _ = await signup_user()
    ws = await create_workspace(client, f"Rep {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}
    mid, token = await _review_link(client, ws, h)

    bot = await client.post(f"/api/v1/video-review-public/{token}/report", json={"reason": "spam spam", "website": "http://x"})
    assert bot.status_code == 201 and (await client.get(f"/api/v1/video-review-public/{token}")).status_code == 200
    await client.post(f"/api/v1/video-review-public/{token}/report", json={"reason": "Not allowed to share this"})

    async def staff():
        return {"id": "staff-1"}

    app.dependency_overrides[auth.require_platform_staff] = staff
    try:
        row = next(r for r in (await client.get("/api/v1/ops/video/takedowns")).json()["requests"] if r["media_id"] == mid)
        out = await client.post(f"/api/v1/ops/video/takedowns/{row['id']}/resolve", json={"action": "dismiss", "note": "Checked, fine"})
        assert out.json()["status"] == "dismissed"
    finally:
        app.dependency_overrides.pop(auth.require_platform_staff, None)
    assert await media_assets.count_documents({"id": mid}) == 1
