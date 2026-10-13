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

from app.core import limits

logger = logging.getLogger(__name__)

_MODEL = "u2netp"
_session = None
_lock = threading.Lock()
#: The last few masks, by picture. A mask is small (the model works on a reduced copy), so keeping several costs almost nothing.
#: Applying a different look to the same picture reuses it instead of running the model again.
_CACHE_SIZE = 12
_cache: "OrderedDict[str, bytes]" = OrderedDict()
#: The model works on a copy no larger than this on its long side; the mask is then stretched to the picture's size.
_WORK_SIDE = 640
#: Free memory the model needs while it runs (measured: about 250 MB above what the server already holds).
_NEEDED_FREE_MB = limits.BG_REMOVAL_FREE_MB


def available() -> bool:
    """True when the library is installed (the model itself is fetched on first use)."""
    try:
        import rembg  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def free_memory_mb() -> Optional[float]:
    """Memory left before this server's limit, or None when the limit cannot be read (a laptop, not a container)."""
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            raw = f.read().strip()
        if raw == "max":
            return None
        with open("/sys/fs/cgroup/memory.current") as f:
            used = int(f.read().strip())
        return (int(raw) - used) / 1048576
    except (OSError, ValueError):
        return None


def _get_session():
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort
            from rembg import new_session

            # No pre-reserved memory pool and one thread: slower by a second or two, but a far smaller footprint.
            options = ort.SessionOptions()
            options.enable_cpu_mem_arena = False
            options.enable_mem_pattern = False
            options.intra_op_num_threads = 1
            _session = new_session(_MODEL, sess_opts=options)
        return _session


def _mask_for(image_bytes: bytes) -> Optional[bytes]:
    key = hashlib.sha1(image_bytes).hexdigest()
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    free = free_memory_mb()
    if free is not None and free < _NEEDED_FREE_MB:
        logger.warning("Background removal skipped: only %.0f MB of memory free", free)
        return None
    from rembg import remove

    with Image.open(io.BytesIO(image_bytes)) as source:
        small = source.convert("RGB")
    small.thumbnail((_WORK_SIDE, _WORK_SIDE))
    mask = remove(small, session=_get_session(), only_mask=True)
    out = io.BytesIO()
    mask.convert("L").save(out, format="PNG")
    data = out.getvalue()
    with _lock:
        _cache[key] = data
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return data


def cut_out(image_bytes: bytes) -> Optional[bytes]:
    """The picture's subject with a transparent background, as PNG bytes, or None when it could not be done."""
    try:
        mask_png = _mask_for(image_bytes)
        if mask_png is None:
            return None
        with Image.open(io.BytesIO(image_bytes)) as source:
            subject = source.convert("RGBA")
        with Image.open(io.BytesIO(mask_png)) as mask:
            subject.putalpha(mask.convert("L").resize(subject.size, Image.LANCZOS))
        out = io.BytesIO()
        subject.save(out, format="PNG")
        return out.getvalue()
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


def fit_cutout(cutout_png: bytes, original_bytes: bytes) -> Optional[bytes]:
    """A cutout made elsewhere (the member's browser), as PNG bytes the size of the original picture, or None when it is not a
    usable picture with a see-through background."""
    try:
        with Image.open(io.BytesIO(original_bytes)) as original:
            size = original.size
        with Image.open(io.BytesIO(cutout_png)) as cutout:
            subject = cutout.convert("RGBA")
        low, high = subject.getchannel("A").getextrema()
        if low == 255 or high == 0:
            return None  # nothing was cut out (all visible), or nothing was found (all gone)
        if subject.size != size:
            subject = subject.resize(size, Image.LANCZOS)
        out = io.BytesIO()
        subject.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - reported to the member, never raised from here
        logger.warning("A browser cutout could not be used: %s", exc)
        return None
