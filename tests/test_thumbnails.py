from types import SimpleNamespace

from app.api.v1 import thumbnails
from app.db.mongo import audio_assets, brand_profiles
from tests.conftest import create_workspace, signup_new_user

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _h(ws: str) -> dict:
    return {"X-Workspace-Id": ws}


async def _setup(api_client, monkeypatch):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Thumbs", tier="large")

    async def fake_upload(data, content_type, user_id, filename=None):
        return "https://img.example/mine.png"

    async def fake_generate(**kwargs):
        return SimpleNamespace(url="https://img.example/made.jpg", qa_flagged=False)

    monkeypatch.setattr(thumbnails, "upload_file", fake_upload)
    monkeypatch.setattr(thumbnails, "generate_brand_image", fake_generate)
    await brand_profiles.insert_one({"id": "b1", "workspace_id": ws})
    await audio_assets.insert_one({
        "id": "a1", "workspace_id": ws, "brand_id": "b1", "title": "Why we feel behind",
        "video_clips": [{"id": "c1", "title": "Short cut"}],
    })
    return ws


async def test_upload_generate_and_clear_for_a_recording_and_a_video(api_client, monkeypatch):
    ws = await _setup(api_client, monkeypatch)

    up = await api_client.post(
        "/api/v1/thumbnails/upload", data={"kind": "audio", "item_id": "a1"},
        files={"file": ("t.png", PNG, "image/png")}, headers=_h(ws),
    )
    assert up.status_code == 200, up.text
    assert (await audio_assets.find_one({"id": "a1"}))["thumbnail_url"] == "https://img.example/mine.png"
    listed = (await api_client.get("/api/v1/audio-assets/", headers=_h(ws))).json()["items"]
    assert listed[0]["thumbnail_url"] == "https://img.example/mine.png"

    gen = await api_client.post("/api/v1/thumbnails/generate", json={"kind": "video", "item_id": "a1", "clip_id": "c1"}, headers=_h(ws))
    assert gen.status_code == 200, gen.text
    clip = (await audio_assets.find_one({"id": "a1"}))["video_clips"][0]
    assert clip["thumbnail_url"] == "https://img.example/made.jpg"

    cleared = await api_client.post("/api/v1/thumbnails/clear", json={"kind": "audio", "item_id": "a1"}, headers=_h(ws))
    assert cleared.status_code == 200
    assert "thumbnail_url" not in (await audio_assets.find_one({"id": "a1"}))


async def test_wrong_file_type_and_missing_items_are_refused_plainly(api_client, monkeypatch):
    ws = await _setup(api_client, monkeypatch)
    bad = await api_client.post(
        "/api/v1/thumbnails/upload", data={"kind": "audio", "item_id": "a1"},
        files={"file": ("t.gif", b"GIF89a", "image/gif")}, headers=_h(ws),
    )
    assert bad.status_code == 400 and "JPEG" in bad.json()["detail"]
    missing = await api_client.post("/api/v1/thumbnails/generate", json={"kind": "audio", "item_id": "nope"}, headers=_h(ws))
    assert missing.status_code == 404
