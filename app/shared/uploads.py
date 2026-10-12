"""Reading an uploaded file without ever holding more than its limit.

`await file.read()` pulls the whole file into one bytes object whatever its size; on a 512 MB server a few big uploads at once
are enough to take everything down. These helpers read in small pieces and stop as soon as the limit is passed, so an oversize
file is refused after at most `max_bytes` plus one piece. The request body itself is also capped, per route, by
`app.core.middleware.MaxBodySizeMiddleware`.
"""
from __future__ import annotations

import io
import tempfile
from typing import BinaryIO, Optional

from fastapi import HTTPException, UploadFile
from PIL import Image

CHUNK_BYTES = 1024 * 1024
#: Pictures larger than this many pixels are refused. The picture's file size says little: a few MB of PNG can hold a
#: 12000 x 12000 picture, which takes about 570 MB once decoded.
MAX_IMAGE_PIXELS = 40_000_000
#: Kept in memory up to this size; larger files in a temporary file on disk.
SPOOL_MEMORY_BYTES = 2 * 1024 * 1024

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
