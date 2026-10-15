"""A video made in the editor goes onto a post as its latest finished render, goes stale when the project changes, and refreshes
to the newer render."""
from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import content_pieces, get_db
from tests.conftest import create_workspace
from tests.test_attachments import H, _attach, _get, _media, _piece


async def _project(ws_id: str, *, rendered: bool = True, version: int = 1) -> tuple[str, str | None]:
    pid = str(uuid4())
    media = await _media(ws_id, "video") if rendered else None
    await get_db()["video_projects"].insert_one({
        "id": pid, "workspace_id": ws_id, "title": "Weekly plan", "version": version, "status": "draft",
        "edit": {"video": [], "audio": [], "text": [], "width": 1080, "height": 1920, "duration_f": 300, "fps": {"num": 30, "den": 1}},
        "created_at": datetime.now(timezone.utc),
        "last_render": {"job_id": "j1", "media_id": media, "preset": "standard", "version": version} if rendered else None,
    })
    return pid, media


async def test_a_finished_video_attaches_and_is_what_gets_published(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "Attach project 1")
    piece = await _piece(ws, profile["id"])
    pid, media = await _project(ws)
    res = await _attach(client, ws, piece, asset_type="project", asset_id=pid)
    assert res.status_code == 200, res.text
    att = res.json()["attachment"]
    assert (att["asset_type"], att["asset_id"], att["media_id"]) == ("project", pid, media)
    assert att["stale"] is False
    doc = await content_pieces.find_one({"piece_id": piece})
    assert [m["id"] for m in doc["media"]] == [media]
    assert [lp["piece_id"] for lp in (await get_db()["video_projects"].find_one({"id": pid}))["linked_pieces"]] == [piece]


async def test_a_video_that_has_not_been_made_cannot_be_attached_and_says_so(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "Attach project 2")
    piece = await _piece(ws, profile["id"])
    pid, _ = await _project(ws, rendered=False)
    res = await _attach(client, ws, piece, asset_type="project", asset_id=pid)
    assert res.status_code == 422 and "has not been made yet" in res.json()["detail"]
    missing = await _attach(client, ws, piece, asset_type="project", asset_id="nope")
    assert missing.status_code == 404


async def test_a_changed_project_or_a_newer_render_marks_the_attachment_out_of_date_and_a_refresh_follows_the_render(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "Attach project 3")
    piece = await _piece(ws, profile["id"])
    pid, first = await _project(ws)
    await _attach(client, ws, piece, asset_type="project", asset_id=pid)
    assert (await _get(client, ws, piece))[0]["stale"] is False
    # the project is edited: the video that is attached no longer matches it
    await get_db()["video_projects"].update_one({"id": pid}, {"$set": {"version": 2}})
    stale = (await _get(client, ws, piece))[0]
    assert stale["stale"] is True and stale["stale_reason"] == "asset_changed"
    # the member makes the video again
    second = await _media(ws, "video")
    await get_db()["video_projects"].update_one({"id": pid}, {"$set": {"last_render": {"job_id": "j2", "media_id": second, "preset": "standard", "version": 2}}})
    assert (await _get(client, ws, piece))[0]["stale"] is True            # still the older render
    refreshed = await client.post(f"/api/v1/content/pieces/{piece}/attachments/refresh", json={}, headers=H(ws))
    assert refreshed.status_code == 200, refreshed.text
    now = (await _get(client, ws, piece))[0]
    assert now["media_id"] == second and now["stale"] is False
    assert [m["id"] for m in (await content_pieces.find_one({"piece_id": piece}))["media"]] == [second]


async def test_another_workspaces_video_cannot_be_attached(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "Attach project 4")
    piece = await _piece(ws, profile["id"])
    pid, _ = await _project(f"ws-{uuid4()}")
    res = await _attach(client, ws, piece, asset_type="project", asset_id=pid)
    assert res.status_code == 404


async def test_the_project_list_has_a_picture_and_says_whether_the_video_is_made_and_current(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Attach project 5")
    pid, media = await _project(ws)
    await get_db()["media_assets"].update_one({"id": media}, {"$set": {"poster_url": "https://cdn.example/poster.jpg"}})
    other, _ = await _project(ws, rendered=False)
    res = await client.get("/api/v1/video-projects", headers=H(ws))
    assert res.status_code == 200, res.text
    by_id = {p["id"]: p for p in res.json()["projects"]}
    assert by_id[pid]["thumbnail_url"] == "https://cdn.example/poster.jpg" and by_id[pid]["made"] is True and by_id[pid]["made_current"] is True
    assert by_id[other]["made"] is False and by_id[other]["thumbnail_url"] is None
    await get_db()["video_projects"].update_one({"id": pid}, {"$set": {"version": 3}})
    again = {p["id"]: p for p in (await client.get("/api/v1/video-projects", headers=H(ws))).json()["projects"]}
    assert again[pid]["made_current"] is False                         # edited since it was made


async def test_a_posted_video_has_results_through_the_posts_it_went_out_in(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "Attach project 6")
    piece = await _piece(ws, profile["id"])
    pid, _ = await _project(ws)
    await _attach(client, ws, piece, asset_type="project", asset_id=pid)
    res = await client.get(f"/api/v1/analytics/assets/project/{pid}", headers=H(ws))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["asset_type"] == "project" and body["post_count"] == 1 and body["measured_posts"] == 0
