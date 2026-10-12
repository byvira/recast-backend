"""The shared upload readers (app.shared.uploads): they stop at the limit and refuse oversize pictures."""
import io

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image

from app.shared import uploads


def _upload(data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename="f.bin")


async def test_read_limited_returns_a_file_within_the_limit():
    assert await uploads.read_limited(_upload(b"a" * 1000), 2000) == b"a" * 1000


async def test_read_limited_refuses_past_the_limit_and_stops_reading():
    seen = {"n": 0}

    class Counting(io.BytesIO):
        def read(self, size=-1):
            chunk = super().read(size)
            seen["n"] += len(chunk)
            return chunk

    file = UploadFile(file=Counting(b"a" * (10 * uploads.CHUNK_BYTES)), filename="f.bin")
    with pytest.raises(HTTPException) as err:
        await uploads.read_limited(file, uploads.CHUNK_BYTES // 2, detail="Too big.")
    assert err.value.status_code == 400 and err.value.detail == "Too big."
    assert seen["n"] <= 2 * uploads.CHUNK_BYTES  # never pulled the whole 10 MB


async def test_spool_limited_copies_then_rewinds_and_refuses_oversize():
    spool = await uploads.spool_limited(_upload(b"hello" * 100), 10_000)
    assert spool.read() == b"hello" * 100
    spool.close()
    with pytest.raises(HTTPException):
        await uploads.spool_limited(_upload(b"x" * 5000), 100)


def _png(size):
    out = io.BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(out, format="PNG")
    return out.getvalue()


def test_assert_image_size_reads_the_header_and_refuses_huge_pictures():
    assert uploads.assert_image_size(_png((40, 30))) == (40, 30)
    with pytest.raises(HTTPException) as err:
        uploads.assert_image_size(_png((300, 200)), max_pixels=1000)
    assert "too large" in err.value.detail


def test_assert_image_size_refuses_a_file_that_is_not_a_picture():
    with pytest.raises(HTTPException) as err:
        uploads.assert_image_size(b"not a picture at all")
    assert "isn't a picture" in err.value.detail


def test_pillow_is_set_to_refuse_decompression_bombs():
    from PIL import Image as PILImage

    assert PILImage.MAX_IMAGE_PIXELS == uploads.MAX_IMAGE_PIXELS


def test_a_workspace_limit_is_never_above_the_hard_ceiling():
    assert uploads.clamp_upload_limit(200 * 1024 * 1024) == uploads.HARD_UPLOAD_BYTES
    assert uploads.clamp_upload_limit(5 * 1024 * 1024) == 5 * 1024 * 1024


async def test_big_uploads_take_turns_and_small_ones_do_not_wait():
    import asyncio

    big = UploadFile(file=io.BytesIO(b""), filename="b", size=uploads.LARGE_UPLOAD_BYTES + 1)
    small = UploadFile(file=io.BytesIO(b""), filename="s", size=100)
    running = {"now": 0, "peak": 0}

    async def job(f):
        async with uploads.large_upload_slot(f):
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
            await asyncio.sleep(0.05)
            running["now"] -= 1

    await asyncio.gather(job(big), job(big), job(big))
    assert running["peak"] == 1
    running["peak"] = 0
    await asyncio.gather(job(small), job(small), job(small))
    assert running["peak"] == 3


async def test_a_link_that_trickles_is_stopped_by_the_total_deadline(monkeypatch):
    import asyncio

    from app.pipelines.audio import link_fetch

    async def slow(url, max_bytes):
        await asyncio.sleep(5)

    monkeypatch.setattr(link_fetch, "_get_bytes", slow)
    monkeypatch.setattr(link_fetch, "TOTAL_FETCH_SECONDS", 0.05)
    with pytest.raises(link_fetch.LinkFetchError) as err:
        await link_fetch.fetch_audio("https://example.com/a.mp3", 1000)
    assert "too long" in str(err.value)
