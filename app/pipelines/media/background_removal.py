"""Taking the background out of a picture, so the subject stands on the brand colour instead.

Runs on this server with a small free model (rembg's u2netp, about 5 MB, fetched the first time it is used), so it needs no paid
service. It works on one picture at a time and takes a few seconds on a normal server CPU."""
from __future__ import annotations

import hashlib
import io
import logging
from collections import OrderedDict
import threading
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

_MODEL = "u2netp"
_session = None
_lock = threading.Lock()
#: The last few cut-outs, by picture. Applying a different look to the same picture reuses the cut-out instead of running the
#: model again (the slow part).
_CACHE_SIZE = 6
_cache: "OrderedDict[str, bytes]" = OrderedDict()


def available() -> bool:
    """True when the library is installed (the model itself is fetched on first use)."""
    try:
        import rembg  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _get_session():
    global _session
    with _lock:
        if _session is None:
            from rembg import new_session

            _session = new_session(_MODEL)
        return _session


def cut_out(image_bytes: bytes) -> Optional[bytes]:
    """The picture's subject with a transparent background, as PNG bytes, or None when it could not be done."""
    key = hashlib.sha1(image_bytes).hexdigest()
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    try:
        from rembg import remove

        out = remove(image_bytes, session=_get_session())
        if not isinstance(out, bytes):
            return None
        with _lock:
            _cache[key] = out
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
        return out
    except Exception as exc:  # noqa: BLE001 - a failure is reported to the member, never raised from here
        logger.warning("Background removal failed: %s", exc)
        return None


def on_colour(cutout_png: bytes, colour_hex: str) -> bytes:
    """`cutout_png` (a transparent-background picture) laid on a solid colour, as PNG bytes."""
    subject = Image.open(io.BytesIO(cutout_png)).convert("RGBA")
    try:
        value = (colour_hex or "").lstrip("#")
        rgb = (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except (ValueError, IndexError):
        rgb = (28, 24, 20)
    base = Image.new("RGBA", subject.size, (*rgb, 255))
    out = io.BytesIO()
    Image.alpha_composite(base, subject).convert("RGB").save(out, format="PNG")
    return out.getvalue()
