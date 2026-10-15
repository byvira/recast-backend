"""The editor's two Activity Log notes: a finished video and a restored version. One row each, updated rather than doubled."""
from uuid import uuid4

from app.db.mongo import activity_entries, get_db
from app.video import activity


async def _project(ws_id: str, title: str = "Launch teaser") -> str:
    pid = uuid4().hex
    await get_db()["video_projects"].insert_one({"id": pid, "workspace_id": ws_id, "title": title})
    return pid


async def test_a_finished_video_is_noted_once_with_a_link_back_to_its_project():
    ws_id = f"ws-{uuid4()}"
    pid = await _project(ws_id)

    await activity.video_made(ws_id, None, pid, "job-1", duration_s=31.4, origin="own")
    await activity.video_made(ws_id, None, pid, "job-1", duration_s=31.4, origin="own")

    rows = await activity_entries.find({"workspace_id": ws_id}).to_list(length=10)
    assert len(rows) == 1
    assert rows[0]["title"] == "Made the video Launch teaser" and "31 seconds" in rows[0]["description"]
    assert rows[0]["href"].endswith(f"?project={pid}") and rows[0]["target_id"] == pid


async def test_a_restore_is_noted_and_says_nothing_was_lost():
    ws_id = f"ws-{uuid4()}"
    pid = await _project(ws_id)

    await activity.version_restored(ws_id, None, pid, 3, 7)

    [row] = await activity_entries.find({"workspace_id": ws_id}).to_list(length=10)
    assert row["title"] == "Restored Launch teaser to version 3"
    assert "version 7" in row["description"] and "still there" in row["description"]


async def test_a_job_without_a_project_makes_no_note():
    ws_id = f"ws-{uuid4()}"
    await activity.video_made(ws_id, None, None, "job-2", duration_s=5)
    assert await activity_entries.count_documents({"workspace_id": ws_id}) == 0


async def test_a_manual_save_and_a_fresh_start_are_noted_but_each_only_once():
    ws_id = f"ws-{uuid4()}"
    pid = await _project(ws_id)

    await activity.version_saved(ws_id, None, pid, 4)
    await activity.version_saved(ws_id, None, pid, 4)
    await activity.started_again(ws_id, None, pid, 5)

    titles = sorted(r["title"] for r in await activity_entries.find({"workspace_id": ws_id}).to_list(length=10))
    assert titles == ["Saved Launch teaser", "Started Launch teaser again"]


async def test_a_render_that_did_not_end_in_a_video_says_why_and_a_finished_one_is_not_noted_here():
    ws_id = f"ws-{uuid4()}"
    pid = await _project(ws_id)

    await activity.render_ended(ws_id, None, pid, "j1", "failed", "The render host is busy.")
    await activity.render_ended(ws_id, None, pid, "j2", "cancelled", "")
    await activity.render_ended(ws_id, None, pid, "j3", "done", "")

    rows = {r["title"]: r for r in await activity_entries.find({"workspace_id": ws_id}).to_list(length=10)}
    assert set(rows) == {"The video Launch teaser could not be made", "The video Launch teaser was cancelled"}
    assert rows["The video Launch teaser could not be made"]["status"] == "failed"
    assert rows["The video Launch teaser could not be made"]["description"] == "The render host is busy."
    assert rows["The video Launch teaser was cancelled"]["status"] == "warning"
