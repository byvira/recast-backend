"""The cut-out is reused, and the Cloudinary calls no longer block the server."""
import asyncio
import threading

import pytest

from app.pipelines.media import background_removal
from app.shared import storage


def _png(size=(40, 30)):
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(out, format="PNG")
    return out.getvalue()


def test_mask_is_reused_for_the_same_picture(monkeypatch):
    from PIL import Image

    calls = []
    monkeypatch.setattr(background_removal, "_get_session", lambda: object())
    monkeypatch.setattr(background_removal, "free_memory_mb", lambda: None)
    import rembg

    monkeypatch.setattr(rembg, "remove", lambda img, session=None, only_mask=False: calls.append(1) or Image.new("L", img.size, 255))
    background_removal._cache.clear()
    first = background_removal.cut_out(_png())
    assert first and background_removal.cut_out(_png())
    assert len(calls) == 1
    background_removal.cut_out(_png((50, 50)))
    assert len(calls) == 2


def test_removal_is_declined_when_memory_is_short(monkeypatch):
    background_removal._cache.clear()
    monkeypatch.setattr(background_removal, "free_memory_mb", lambda: 120)
    assert background_removal.cut_out(_png()) is None


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
