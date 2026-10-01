"""Opening existing work again: one image or audio project by id (for "Open in pipeline"), and the video history."""
from datetime import datetime, timezone

from app.db.mongo import audio_assets, media_assets
from tests.test_audio_assets import _generate as _generate_audio, _h, _setup as _setup_audio, stubs as audio_stubs  # noqa: F401 — fixture reuse
from tests.test_image_assets import _generate as _generate_image, _setup as _setup_image, stubs as image_stubs  # noqa: F401 — fixture reuse


async def test_an_image_project_opens_again_with_its_prompt_settings_and_layers(signup_user, image_stubs):  # noqa: F811
    client, _, ws_id, brand_id = await _setup_image(signup_user)
    made = (await _generate_image(client, ws_id, brand_id, negative_prompt="faces, clutter")).json()
    res = await client.get(f"/api/v1/image-assets/{made['id']}", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == made["id"] and body["prompt"] == "a calm sunrise over hills" and body["negative_prompt"] == "faces, clutter"
    assert body["slides"][0]["text_content"]["headline"] == "Clarity beats scale" and len(body["slides"][0]["layers"]) >= 2
    assert (await client.get("/api/v1/image-assets/does-not-exist", headers=_h(ws_id))).status_code == 404


async def test_an_image_project_cannot_be_opened_from_another_workspace(signup_user, image_stubs):  # noqa: F811
    client, _, ws_id, brand_id = await _setup_image(signup_user)
    made = (await _generate_image(client, ws_id, brand_id)).json()
    other_client, _, other_ws, _ = await _setup_image(signup_user)
    assert (await other_client.get(f"/api/v1/image-assets/{made['id']}", headers=_h(other_ws))).status_code == 404


async def test_the_fixed_image_paths_still_win_over_the_open_by_id_route(signup_user, image_stubs):  # noqa: F811
    client, _, ws_id, _ = await _setup_image(signup_user)
    assert (await client.get("/api/v1/image-assets/layouts", headers=_h(ws_id))).status_code == 200
    assert (await client.get("/api/v1/image-assets/icons", headers=_h(ws_id))).status_code == 200
    assert (await client.get("/api/v1/image-assets/", headers=_h(ws_id))).status_code == 200


async def test_an_audio_project_opens_again_and_the_fixed_audio_paths_still_work(signup_user, audio_stubs):  # noqa: F811
    client, _, ws_id, brand_id = await _setup_audio(signup_user)
    made = (await _generate_audio(client, ws_id, brand_id)).json()
    res = await client.get(f"/api/v1/audio-assets/{made['id']}", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["id"] == made["id"] and res.json()["brand_id"] == brand_id
    assert (await client.get("/api/v1/audio-assets/nope", headers=_h(ws_id))).status_code == 404
    assert (await client.get("/api/v1/audio-assets/video-presets", headers=_h(ws_id))).status_code == 200
    assert (await client.get("/api/v1/audio-assets/", headers=_h(ws_id))).status_code == 200


async def test_the_video_history_lists_every_video_with_its_file_and_its_recording(signup_user, audio_stubs):  # noqa: F811
    client, _, ws_id, brand_id = await _setup_audio(signup_user)
    made = (await _generate_audio(client, ws_id, brand_id)).json()
    empty = (await client.get("/api/v1/audio-assets/video-clips", headers=_h(ws_id))).json()
    assert empty["items"] == []

    media = await media_assets.find_one({"id": made["media_id"]})
    clip_media = {**{k: v for k, v in media.items() if k != "_id"}, "id": "video-media-1", "kind": "video", "mime_type": "video/mp4", "url": "https://res.cloudinary.com/demo/video/v1/a.mp4"}
    await media_assets.insert_one(clip_media)
    clip = {"id": "c1", "media_id": "video-media-1", "start_s": 0.0, "end_s": 5.0, "style": "waveform", "size": "vertical", "title": "My clip",
            "platform": "linkedin", "notes": [], "created_by": "u", "created_at": datetime.now(timezone.utc)}
    await audio_assets.update_one({"id": made["id"]}, {"$push": {"video_clips": clip}})

    body = (await client.get("/api/v1/audio-assets/video-clips", headers=_h(ws_id))).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["audio_asset_id"] == made["id"] and item["title"] == "My clip" and item["media"]["url"].endswith("a.mp4") and item["size"] == "vertical"

    other_client, _, other_ws, _ = await _setup_audio(signup_user)
    assert (await other_client.get("/api/v1/audio-assets/video-clips", headers=_h(other_ws))).json()["items"] == []


async def test_a_text_session_keeps_what_was_typed_so_it_can_be_opened_again():
    from uuid import uuid4

    from app.pipelines.text.storage import MAX_INPUT_TEXT, ensure_session_exists, get_session, get_workspace_sessions

    ws, sid = f"ws-{uuid4().hex[:8]}", uuid4().hex
    long_input = "Tone drift costs teams hours every week. " * 400
    await ensure_session_exists(session_id=sid, workspace_id=ws, user_id="u1", brand_id="b1", source_type="text", input_text=long_input)
    await ensure_session_exists(session_id=sid, workspace_id=ws, user_id="u1", brand_id="b1", source_type="text", input_text="a later call must not overwrite it")

    detail = await get_session(sid, ws)
    assert detail["input_text"].startswith("Tone drift costs teams") and len(detail["input_text"]) == MAX_INPUT_TEXT

    listed = await get_workspace_sessions(ws)
    item = next(i for i in listed["items"] if i["session_id"] == sid)
    assert item["preview"].startswith("Tone drift costs teams hours") and len(item["preview"]) <= 160 and "input_text" not in item
    assert listed["total"] == 1 and listed["has_more"] is False
