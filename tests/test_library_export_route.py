"""GET /api/v1/content/export?format=zip: posts as .txt, media in native formats, strict names.

Real database; only the media download is stubbed (no network)."""

import io
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

from app.api.v1 import content as content_module
from app.db.mongo import audio_assets, image_assets, media_assets
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace
from tests.test_image_assets import _brand


async def _setup(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Export WS")
    brand_id = await _brand(client, ws_id)
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"], brand_id=brand_id, source_type="text",
    )
    await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"], brand_id=brand_id,
        platform="LinkedIn", content="Why systems beat willpower\nBody.", word_count=4, char_count=30,
    )
    return client, ws_id, brand_id


async def _add_audio(ws_id, brand_id, url="https://files.test/ep.mp3"):
    media_id = uuid4().hex
    now = datetime.now(timezone.utc)
    await media_assets.insert_one({
        "id": media_id, "workspace_id": ws_id, "kind": "audio", "url": url, "mime_type": "audio/mpeg",
        "source": "synthesized", "created_by": "u", "created_at": now, "size_bytes": 4,
    })
    await audio_assets.insert_one({
        "id": uuid4().hex, "workspace_id": ws_id, "brand_id": brand_id, "title": "Episode 1",
        "source_type": "script_tts", "media_id": media_id, "created_at": now,
    })


def _zip(res):
    assert res.status_code == 200, res.text
    return zipfile.ZipFile(io.BytesIO(res.content))


async def test_zip_has_txt_posts_and_native_media_with_the_required_names(signup_user, monkeypatch):
    client, ws_id, brand_id = await _setup(signup_user)
    await _add_audio(ws_id, brand_id)

    async def fake_fetch(url, client, limit):
        return b"AUDIO-BYTES"

    monkeypatch.setattr(content_module, "_fetch_media", fake_fetch)
    zf = _zip(await client.get("/api/v1/content/export?format=zip", headers={"X-Workspace-Id": ws_id}))

    names = zf.namelist()
    assert "text/why-systems-beat-willpower_linkedin_text_post.txt" in names
    assert "media/episode-1_library_audio_narration.mp3" in names
    assert zf.read("media/episode-1_library_audio_narration.mp3") == b"AUDIO-BYTES"
    assert not any(n.endswith(".md") for n in names)
    assert "README.txt" in names


async def test_a_file_that_cannot_be_downloaded_is_reported_not_fatal(signup_user, monkeypatch):
    client, ws_id, brand_id = await _setup(signup_user)
    await _add_audio(ws_id, brand_id)

    async def failing_fetch(url, client, limit):
        return None

    monkeypatch.setattr(content_module, "_fetch_media", failing_fetch)
    zf = _zip(await client.get("/api/v1/content/export?format=zip", headers={"X-Workspace-Id": ws_id}))
    assert not any(n.startswith("media/") for n in zf.namelist())
    assert "Episode 1" in zf.read("README.txt").decode()
    assert any(n.startswith("text/") for n in zf.namelist())


async def test_another_workspaces_media_is_never_included(signup_user, monkeypatch):
    client, ws_id, brand_id = await _setup(signup_user)
    await _add_audio("some-other-workspace", "b", url="https://files.test/secret.mp3")
    seen = []

    async def fake_fetch(url, client, limit):
        seen.append(url)
        return b"x"

    monkeypatch.setattr(content_module, "_fetch_media", fake_fetch)
    zf = _zip(await client.get("/api/v1/content/export?format=zip", headers={"X-Workspace-Id": ws_id}))
    assert seen == []
    assert not any(n.startswith("media/") for n in zf.namelist())


async def test_markdown_and_csv_still_work(signup_user):
    client, ws_id, _ = await _setup(signup_user)
    md = await client.get("/api/v1/content/export?format=markdown", headers={"X-Workspace-Id": ws_id})
    csv_res = await client.get("/api/v1/content/export?format=csv", headers={"X-Workspace-Id": ws_id})
    assert md.status_code == 200 and "Why systems beat willpower" in md.text
    assert csv_res.status_code == 200 and "piece_id" in csv_res.text
