"""What a member can choose when exporting a picture: how much larger it is made (1x, 2x or 4x), the print resolution it is
tagged with (72, 150 or 300 dpi) and whether the file is squeezed to be small. The picture is redrawn larger with a smooth
resize (it cannot invent detail, but it gives a sharper, bigger file), and the dpi is written into the file so print tools read
the right size."""
from __future__ import annotations

import io
from typing import Literal

from PIL import Image

from app.core import limits

UpscaleFactor = Literal[1, 2, 4]
Dpi = Literal[72, 150, 300]

#: A picture is never made larger than this on its longest side, nor than this many pixels in all. A 3000 x 3000 cover at 4x would be
#: 144 million pixels (576 MB once decoded), so the size limit is on the total, not only the longest side.
MAX_SIDE_PX = 8000
MAX_PIXELS = limits.EXPORT_MAX_PIXELS

_FORMATS = {"png": "PNG", "webp": "WEBP", "jpg": "JPEG", "jpeg": "JPEG"}
_MIME = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def mime_for(export_format: str) -> str:
    return _MIME[export_format]


def is_default_export(upscale: int, dpi: int, optimize: bool) -> bool:
    """True when nothing was changed from the standard export, which keeps using the fast format conversion."""
    return upscale == 1 and dpi == 300 and optimize


def _enlarged_image(data: bytes, upscale: int) -> Image.Image:
    """The picture as a decoded image at up to `upscale` times its size, limited by MAX_SIDE_PX and MAX_PIXELS."""
    img = Image.open(io.BytesIO(data))
    img.load()
    if upscale <= 1:
        return img
    width, height = img.size
    factor = min(upscale, MAX_SIDE_PX / max(width, height), (MAX_PIXELS / (width * height)) ** 0.5)
    if factor <= 1:
        return img
    img = img.convert("RGBA") if img.mode in ("P", "LA") else img
    return img.resize((max(1, int(width * factor)), max(1, int(height * factor))), Image.LANCZOS)  # rounded down so the pixel cap holds


def enlarge(data: bytes, upscale: int) -> bytes:
    """The same picture at `upscale` times the size, as PNG bytes. 1x returns it untouched."""
    if upscale <= 1:
        return data
    img = _enlarged_image(data, upscale)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def render_export(data: bytes, *, export_format: str, upscale: int = 1, dpi: int = 300, optimize: bool = True) -> bytes:
    """The picture saved as `export_format` with the chosen size, resolution and file-size setting."""
    if export_format not in _FORMATS:
        raise ValueError(f"Cannot export as {export_format!r}.")
    img = _enlarged_image(data, upscale)  # decoded once; no PNG round trip in between
    fmt = _FORMATS[export_format]
    out = io.BytesIO()
    if fmt == "JPEG":
        img = img.convert("RGB")
        img.save(out, format="JPEG", quality=85 if optimize else 97, optimize=optimize, dpi=(dpi, dpi))
    elif fmt == "WEBP":
        img.save(out, format="WEBP", quality=85 if optimize else 100, method=6 if optimize else 4)
    else:
        img.save(out, format="PNG", optimize=optimize, dpi=(dpi, dpi))
    return out.getvalue()
