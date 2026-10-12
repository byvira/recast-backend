"""The cut-out is reused, and the Cloudinary calls no longer block the server."""
import asyncio
import threading

import pytest

from app.pipelines.media import background_removal
from app.shared import storage


def test_cutout_is_reused_for_the_same_picture(monkeypatch):
    calls = []
    monkeypatch.setattr(background_removal, "_get_session", lambda: object())
    import rembg

    monkeypatch.setattr(rembg, "remove", lambda data, session=None: calls.append(1) or b"png")
    background_removal._cache.clear()
    assert background_removal.cut_out(b"picture") == b"png"
    assert background_removal.cut_out(b"picture") == b"png"
    assert len(calls) == 1
    background_removal.cut_out(b"another")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_upload_runs_off_the_server_loop(monkeypatch):
    seen = {}
    monkeypatch.setattr(storage, "_ensure_configured", lambda: None)

    def fake_upload(*a, **k):
        seen["thread"] = threading.current_thread()
        return {"secure_url": "https://x/y.png"}

    monkeypatch.setattr(storage.cloudinary.uploader, "upload", fake_upload)
    out = await storage.upload_file_detailed(b"x", storage.ContentType.IMAGE, "u1")
    assert out["url"] == "https://x/y.png"
    assert seen["thread"] is not threading.main_thread()
