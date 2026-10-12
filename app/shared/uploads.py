"""Reading an uploaded file without ever holding more than its limit.

`await file.read()` pulls the whole file into one bytes object whatever its size; on a 512 MB server a few big uploads at once
are enough to take everything down. These helpers read in small pieces and stop as soon as the limit is passed, so an oversize
file is refused after at most `max_bytes` plus one piece. The request body itself is also capped, per route, by
`app.core.middleware.MaxBodySizeMiddleware`.
"""
from __future__ import annotations

import asyncio
import io
import tempfile
from contextlib import asynccontextmanager
from typing import BinaryIO, Optional

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

from app.core import limits

CHUNK_BYTES = 1024 * 1024
#: Pictures larger than this many pixels are refused. The picture's file size says little: a few MB of PNG can hold a
#: 12000 x 12000 picture, which takes about 570 MB once decoded.
MAX_IMAGE_PIXELS = limits.IMAGE_MAX_PIXELS
#: Kept in memory up to this size; larger files in a temporary file on disk.
SPOOL_MEMORY_BYTES = 2 * 1024 * 1024

#: The most one upload may be, whatever a workspace's own limit says. A file read for processing is held in memory about twice
#: while it is copied on to storage, and the server has 512 MB in all.
HARD_UPLOAD_BYTES = limits.UPLOAD_HARD_LIMIT_BYTES
#: Files at least this big are handled one at a time, so a few big uploads at once cannot add up past the memory.
LARGE_UPLOAD_BYTES = limits.LARGE_UPLOAD_BYTES
_large_upload_gate = asyncio.Semaphore(1)


def clamp_upload_limit(limit_bytes: int) -> int:
    """A workspace's own upload limit, never above the server's hard ceiling."""
    return min(limit_bytes, HARD_UPLOAD_BYTES)


@asynccontextmanager
async def large_upload_slot(file: UploadFile):
    """Held while a big upload is read and handled. Small files pass straight through; big ones wait their turn."""
    if (getattr(file, "size", None) or 0) < LARGE_UPLOAD_BYTES:
        yield
        return
    async with _large_upload_gate:
        yield


# Make Pillow itself refuse (not just warn about) a decompression bomb anywhere it decodes.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def _too_big(detail: Optional[str], max_bytes: int, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail or f"This file must be {max_bytes // (1024 * 1024)}MB or smaller.",
    )


async def read_limited(file: UploadFile, max_bytes: int, *, detail: Optional[str] = None, status_code: int = 400) -> bytes:
    """The file's bytes, or an HTTP error when it is larger than `max_bytes`. Never reads past the limit plus one piece."""
    out = bytearray()
    while True:
        chunk = await file.read(CHUNK_BYTES)
        if not chunk:
            break
        out += chunk
        if len(out) > max_bytes:
            raise _too_big(detail, max_bytes, status_code)
    return bytes(out)


async def spool_limited(file: UploadFile, max_bytes: int, *, detail: Optional[str] = None, status_code: int = 400) -> BinaryIO:
    """The file copied into a temporary file (disk beyond a couple of MB), rewound to the start. For work that carries on after
    the request has ended, when the upload itself is closed. The caller closes it when done."""
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES)
    total = 0
    try:
        while True:
            chunk = await file.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _too_big(detail, max_bytes, status_code)
            spool.write(chunk)
        spool.seek(0)
        return spool
    except BaseException:
        spool.close()
        raise


def upright(data: bytes) -> bytes:
    """The picture turned the way it was held when taken. A phone photo is stored sideways with a note saying how to turn it; anything
    that ignores the note (drawing text on it, applying looks, cropping) shows it on its side. Returned unchanged when there is no
    note or the file cannot be read."""
    try:
        with Image.open(io.BytesIO(data)) as picture:
            if (picture.getexif() or {}).get(0x0112, 1) in (None, 1):
                return data
            fixed = ImageOps.exif_transpose(picture)
            kind = picture.format or "JPEG"
            out = io.BytesIO()
            fixed.save(out, format=kind, **({"quality": 95} if kind in ("JPEG", "WEBP") else {}))
            return out.getvalue()
    except Exception:  # noqa: BLE001 - a file that cannot be turned is kept as it came
        return data


def assert_image_size(data: bytes, *, max_pixels: int = MAX_IMAGE_PIXELS) -> tuple[int, int]:
    """(width, height) of a picture, read from its header without decoding it. A picture with too many pixels, or one that
    cannot be read as a picture, is refused with a plain message."""
    try:
        with Image.open(io.BytesIO(data)) as picture:
            width, height = picture.size
    except Image.DecompressionBombError as exc:
        raise HTTPException(
            status_code=400, detail=f"This picture is too large. Use one under {max_pixels // 1_000_000} megapixels."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - any unreadable file is the same answer to the member
        raise HTTPException(status_code=400, detail="This file isn't a picture we can read.") from exc
    if width * height > max_pixels:
        raise HTTPException(
            status_code=400,
            detail=f"This picture is too large ({width} x {height}). Use one under {max_pixels // 1_000_000} megapixels.",
        )
    return width, height
