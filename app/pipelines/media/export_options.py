"""What a member can choose when exporting a picture: how much larger it is made (1x, 2x or 4x), the print resolution it is
tagged with (72, 150 or 300 dpi) and whether the file is squeezed to be small. The picture is redrawn larger with a smooth
resize (it cannot invent detail, but it gives a sharper, bigger file), and the dpi is written into the file so print tools read
the right size."""
from __future__ import annotations

import io
from typing import Literal

from PIL import Image

UpscaleFactor = Literal[1, 2, 4]
Dpi = Literal[72, 150, 300]

#: A picture is never made larger than this on its longest side, so a large source at 4x cannot use up the server's memory.
MAX_SIDE_PX = 8000

_FORMATS = {"png": "PNG", "webp": "WEBP", "jpg": "JPEG", "jpeg": "JPEG"}
_MIME = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def mime_for(export_format: str) -> str:
    return _MIME[export_format]


def is_default_export(upscale: int, dpi: int, optimize: bool) -> bool:
    """True when nothing was changed from the standard export, which keeps using the fast format conversion."""
    return upscale == 1 and dpi == 300 and optimize


def enlarge(data: bytes, upscale: int) -> bytes:
    """The same picture at `upscale` times the size, as PNG bytes. 1x returns it untouched."""
    if upscale <= 1:
        return data
    img = Image.open(io.BytesIO(data))
    img.load()
    width, height = img.size
    factor = min(upscale, MAX_SIDE_PX / max(width, height))
    if factor <= 1:
        return data
    img = img.convert("RGBA") if img.mode in ("P", "LA") else img
    img = img.resize((round(width * factor), round(height * factor)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def render_export(data: bytes, *, export_format: str, upscale: int = 1, dpi: int = 300, optimize: bool = True) -> bytes:
    """The picture saved as `export_format` with the chosen size, resolution and file-size setting."""
    if export_format not in _FORMATS:
        raise ValueError(f"Cannot export as {export_format!r}.")
    img = Image.open(io.BytesIO(enlarge(data, upscale)))
    img.load()
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
