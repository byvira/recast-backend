"""Rename, delete, export and save-as-draft for the cards in the History tabs (text runs, picture projects, recordings, videos)."""
from uuid import uuid4

from app.db.mongo import audio_assets, content_pieces, content_sessions, image_assets
from tests.conftest import create_workspace
from tests.test_attachments import H, _audio, _image
from app.pipelines.text.storage import ensure_session_exists, save_live_piece


async def _run(ws_id: str, user_id: str, *, platforms=("LinkedIn", "Bluesky")) -> tuple[str, list[str]]:
    session_id = str(uuid4())
    await ensure_session_exists(session_id=session_id, workspace_id=ws_id, user_id=user_id, brand_id="brand-1", source_type="text", input_text="Some input")
    ids = [
        await save_live_piece(
            session_id=session_id, workspace_id=ws_id, user_id=user_id, brand_id="brand-1", platform=p, content=f"Post for {p}.",
            word_count=4, char_count=20,
        )
        for p in platforms
    ]
    return session_id, ids


# ── Text runs ─────────────────────────────────────────────────────────

async def test_a_text_run_can_be_renamed_and_the_name_shows_in_the_list(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "History Actions 1")
    session_id, _ = await _run(ws, profile["id"])

    res = await client.patch(f"/api/v1/content/sessions/{session_id}", json={"title": "  Launch   week  "}, headers=H(ws))
    assert res.status_code == 200 and res.json()["title"] == "Launch week"
    listed = (await client.get("/api/v1/content/sessions", headers=H(ws))).json()["items"]
    assert next(s for s in listed if s["session_id"] == session_id)["title"] == "Launch week"

    assert (await client.patch(f"/api/v1/content/sessions/{session_id}", json={"title": "   "}, headers=H(ws))).status_code == 422
    assert (await client.patch(f"/api/v1/content/sessions/{session_id}", json={"title": "x" * 121}, headers=H(ws))).status_code == 422
    assert (await client.patch("/api/v1/content/sessions/nope", json={"title": "A"}, headers=H(ws))).status_code == 404


async def test_deleting_a_run_removes_its_unpublished_posts_and_keeps_the_published_ones(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "History Actions 2")
    session_id, (live, draft) = await _run(ws, profile["id"])
    await content_pieces.update_one({"piece_id": live}, {"$set": {"publish_status": "published"}})

    res = await client.delete(f"/api/v1/content/sessions/{session_id}", headers=H(ws))
    assert res.status_code == 200 and res.json()["posts_removed"] == 1 and res.json()["posts_kept"] == 1

    assert (await content_pieces.find_one({"piece_id": draft}))["deleted"] is True
    assert not (await content_pieces.find_one({"piece_id": live})).get("deleted")
    assert (await client.get(f"/api/v1/content/sessions/{session_id}", headers=H(ws))).status_code == 404
    listed = (await client.get("/api/v1/content/sessions", headers=H(ws))).json()
    assert session_id not in [s["session_id"] for s in listed["items"]]


async def test_a_run_with_a_scheduled_post_cannot_be_deleted_until_it_is_cancelled(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "History Actions 3")
    session_id, (first, _) = await _run(ws, profile["id"])
    await content_pieces.update_one({"piece_id": first}, {"$set": {"publish_status": "queued"}})

    res = await client.delete(f"/api/v1/content/sessions/{session_id}", headers=H(ws))
    assert res.status_code == 409 and "Cancel" in res.json()["detail"]
    assert not (await content_sessions.find_one({"session_id": session_id})).get("deleted")


async def test_a_run_exports_as_markdown_and_csv(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "History Actions 4")
    session_id, _ = await _run(ws, profile["id"])

    md = await client.get(f"/api/v1/content/sessions/{session_id}/export", params={"format": "markdown"}, headers=H(ws))
    assert md.status_code == 200 and "Post for LinkedIn." in md.text and "attachment" in md.headers["content-disposition"]
    csv = await client.get(f"/api/v1/content/sessions/{session_id}/export", params={"format": "csv"}, headers=H(ws))
    assert csv.status_code == 200 and "Post for Bluesky." in csv.text
    assert (await client.get("/api/v1/content/sessions/nope/export", params={"format": "csv"}, headers=H(ws))).status_code == 404


# ── Picture projects ──────────────────────────────────────────────────

async def test_a_picture_project_can_be_renamed_and_deleted_and_then_disappears_everywhere(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "History Actions 5")
    image_id, _ = await _image(ws)

    renamed = await client.patch(f"/api/v1/image-assets/{image_id}/rename", json={"title": "Spring sale"}, headers=H(ws))
    assert renamed.status_code == 200 and renamed.json()["title"] == "Spring sale"
    assert (await client.get("/api/v1/image-assets/", headers=H(ws))).json()["items"][0]["title"] == "Spring sale"

    assert (await client.delete(f"/api/v1/image-assets/{image_id}", headers=H(ws))).status_code == 200
    assert (await client.get("/api/v1/image-assets/", headers=H(ws))).json()["total"] == 0
    assert (await client.get(f"/api/v1/image-assets/{image_id}", headers=H(ws))).status_code == 404
    assert (await client.patch(f"/api/v1/image-assets/{image_id}/rename", json={"title": "Again"}, headers=H(ws))).status_code == 404
    assert (await client.delete(f"/api/v1/image-assets/{image_id}", headers=H(ws))).status_code == 404
    # the project is hidden, not erased
    assert (await image_assets.find_one({"id": image_id}))["deleted"] is True


async def test_a_picture_project_is_saved_as_a_draft_once_per_platform(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "History Actions 6", tier="large")
    image_id, media = await _image(ws)

    first = await client.post(f"/api/v1/image-assets/{image_id}/send-to-draft", json={}, headers=H(ws))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True and body["platform"] == "Instagram" and body["content"] == "Card"
    piece = await content_pieces.find_one({"piece_id": body["piece_id"]})
    assert [m["id"] for m in piece["media"]] == [media[0]] and piece["approval_status"] == "pending"

    again = await client.post(f"/api/v1/image-assets/{image_id}/send-to-draft", json={}, headers=H(ws))
    assert again.json()["piece_id"] == body["piece_id"] and again.json()["created"] is False

    other = await client.post(f"/api/v1/image-assets/{image_id}/send-to-draft", json={"platform": "LinkedIn", "caption": "Look"}, headers=H(ws))
    assert other.json()["piece_id"] != body["piece_id"] and other.json()["content"] == "Look"
    # drafts for several platforms from one project share a run, so the publish window shows them together
    sessions = {(await content_pieces.find_one({"piece_id": i}))["session_id"] for i in (body["piece_id"], other.json()["piece_id"])}
    assert len(sessions) == 1

    assert (await client.post(f"/api/v1/image-assets/{image_id}/send-to-draft", json={"platform": "Nowhere"}, headers=H(ws))).status_code == 400
    assert (await client.post("/api/v1/image-assets/nope/send-to-draft", json={}, headers=H(ws))).status_code == 404


async def test_a_deleted_picture_project_cannot_be_saved_as_a_draft(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "History Actions 7")
    image_id, _ = await _image(ws)
    await client.delete(f"/api/v1/image-assets/{image_id}", headers=H(ws))
    assert (await client.post(f"/api/v1/image-assets/{image_id}/send-to-draft", json={}, headers=H(ws))).status_code == 404


# ── Recordings and videos ─────────────────────────────────────────────

async def test_a_recording_can_be_renamed_and_deleted(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "History Actions 8")
    audio = await _audio(ws)

    assert (await client.patch(f"/api/v1/audio-assets/{audio['id']}/rename", json={"title": "Episode 12"}, headers=H(ws))).json()["title"] == "Episode 12"
    assert (await client.get("/api/v1/audio-assets/", headers=H(ws))).json()["items"][0]["title"] == "Episode 12"
    assert (await client.delete(f"/api/v1/audio-assets/{audio['id']}", headers=H(ws))).status_code == 200
    assert (await client.get("/api/v1/audio-assets/", headers=H(ws))).json()["total"] == 0
    assert (await client.delete(f"/api/v1/audio-assets/{audio['id']}", headers=H(ws))).status_code == 404
    assert (await audio_assets.find_one({"id": audio["id"]}))["deleted"] is True


async def test_a_video_can_be_renamed_and_removed_without_touching_the_recording(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "History Actions 9")
    audio = await _audio(ws, with_clip=True)
    base = f"/api/v1/audio-assets/{audio['id']}/video-clips/{audio['clip_id']}"

    assert (await client.patch(base, json={"title": "Teaser"}, headers=H(ws))).json()["title"] == "Teaser"
    listed = (await client.get("/api/v1/audio-assets/video-clips", headers=H(ws))).json()
    assert listed["total"] == 1 and listed["items"][0]["title"] == "Teaser"

    assert (await client.delete(base, headers=H(ws))).status_code == 200
    assert (await client.get("/api/v1/audio-assets/video-clips", headers=H(ws))).json()["total"] == 0
    assert (await client.get("/api/v1/audio-assets/", headers=H(ws))).json()["total"] == 1
    assert (await client.delete(base, headers=H(ws))).status_code == 404
    assert (await client.patch(f"/api/v1/audio-assets/{audio['id']}/video-clips/nope", json={"title": "A"}, headers=H(ws))).status_code == 404


# ── Only approved work can be drafted for posting ─────────────────────

async def test_an_unapproved_picture_or_video_cannot_be_drafted_until_it_is_approved(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "History Actions 10")
    image_id, _ = await _image(ws, approval_status="pending")
    audio = await _audio(ws, with_clip=True, approval_status="pending")

    picture = await client.post(f"/api/v1/image-assets/{image_id}/send-to-draft", json={}, headers=H(ws))
    video = await client.post(f"/api/v1/audio-assets/{audio['id']}/send-to-draft", json={"clip_id": audio["clip_id"]}, headers=H(ws))
    assert picture.status_code == 409 and "Approve" in picture.json()["detail"]
    assert video.status_code == 409 and "Approve" in video.json()["detail"]

    await image_assets.update_one({"id": image_id}, {"$set": {"approval_status": "approved"}})
    await audio_assets.update_one({"id": audio["id"]}, {"$set": {"approval_status": "approved"}})
    assert (await client.post(f"/api/v1/image-assets/{image_id}/send-to-draft", json={}, headers=H(ws))).status_code == 200
    assert (await client.post(f"/api/v1/audio-assets/{audio['id']}/send-to-draft", json={"clip_id": audio["clip_id"]}, headers=H(ws))).status_code == 200


async def test_the_video_history_says_whether_each_video_may_be_posted(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "History Actions 11")
    await _audio(ws, with_clip=True, approval_status="pending")
    items = (await client.get("/api/v1/audio-assets/video-clips", headers=H(ws))).json()["items"]
    assert items[0]["approval_status"] == "pending"
