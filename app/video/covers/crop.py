"""Cutting a frame to a platform's shape without cutting a face, and keeping the saved file under the platform's size limit."""
from __future__ import annotations

import io
from typing import Any, Optional

from PIL import Image, ImageFilter

HEADROOM = 0.18          # of the crop height, kept above the top of the faces


def crop_box(src_w: int, src_h: int, dst_w: int, dst_h: int, faces: Optional[list[dict[str, float]]]) -> tuple[tuple[int, int, int, int], str]:
    """The box to cut from the frame so it has the platform's shape, and how it was chosen: "faces" (centred on the faces),
    "centre" (nothing known, a little above the middle), or "blur" (the faces would not fit, so the whole frame is kept and the
    rest is filled). The box is never outside the frame, and in "faces" mode it never cuts through a face."""
    aspect = dst_w / dst_h
    if src_w / src_h > aspect:
        cw, ch = int(round(src_h * aspect)), src_h
    else:
        cw, ch = src_w, int(round(src_w / aspect))
    cw, ch = min(cw, src_w), min(ch, src_h)
    slack_x, slack_y = src_w - cw, src_h - ch
    if faces:
        x0 = min(f["x"] for f in faces) * src_w
        x1 = max(f["x"] + f["w"] for f in faces) * src_w
        y0 = min(f["y"] for f in faces) * src_h
        y1 = max(f["y"] + f["h"] for f in faces) * src_h
        if x1 - x0 > cw or y1 - y0 > ch:
            return (0, 0, src_w, src_h), "blur"
        # the box has to hold every face: left in [x1 - cw, x0] and top in [y1 - ch, y0], inside the frame
        left = int(round(min(max((x0 + x1) / 2 - cw / 2, max(0, x1 - cw)), min(slack_x, x0))))
        top = int(round(min(max(y0 - HEADROOM * ch, max(0, y1 - ch)), min(slack_y, y0))))
        return (left, top, left + cw, top + ch), "faces"
    return (int(slack_x / 2), int(slack_y * 0.35), int(slack_x / 2) + cw, int(slack_y * 0.35) + ch), "centre"


def fit_frame(data: bytes, dst_w: int, dst_h: int, faces: Optional[list[dict[str, float]]]) -> tuple[bytes, str, dict[str, Any]]:
    """The frame at exactly the platform's size, as a JPEG. Returns (picture, how it was cut, details)."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        box, mode = crop_box(im.width, im.height, dst_w, dst_h, faces)
        if mode == "blur":
            back = im.resize((dst_w, dst_h), Image.LANCZOS)
            back = back.filter(ImageFilter.GaussianBlur(max(8, dst_w // 60)))
            scale = min(dst_w / im.width, dst_h / im.height)
            fg = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
            back.paste(fg, ((dst_w - fg.width) // 2, (dst_h - fg.height) // 2))
            out = back
        else:
            out = im.crop(box).resize((dst_w, dst_h), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue(), mode, {"box": box}


def under_limit(png_or_jpeg: bytes, max_bytes: int) -> tuple[bytes, str]:
    """The picture as a file the platform will take: kept as is when it is small enough, otherwise saved as a JPEG at the best
    quality that fits. Returns (bytes, mime type). Raises ValueError when even the lowest sensible quality is too big."""
    with Image.open(io.BytesIO(png_or_jpeg)) as im:
        is_png = im.format == "PNG"
        if len(png_or_jpeg) <= max_bytes:
            return png_or_jpeg, "image/png" if is_png else "image/jpeg"
        rgb = im.convert("RGB")
        for quality in (90, 82, 74, 66, 58, 50):
            buf = io.BytesIO()
            rgb.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue(), "image/jpeg"
    raise ValueError("The picture cannot be made small enough for this platform.")
