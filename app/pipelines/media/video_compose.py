"""The still picture a video is built on: a brand-coloured background (or the brand's cover with a blurred fill), a short
title, the brand's logo and a darkened lower area so captions stay readable. It is composed once with Pillow, so the
brand's real fonts and logo are used exactly as in the picture pipeline, and ffmpeg only has to add the moving parts
(waveform, progress bar, word-highlighted captions). Nothing here needs a network or a model.

Safe areas keep titles and captions clear of the buttons and captions platforms draw over the picture (the top and
bottom of a vertical video are the worst)."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.pipelines.media.image_render import _hex_or_default, _load_font, _rgb, _wrap_text

logger = logging.getLogger(__name__)

# (top, bottom) as a fraction of the height that titles and captions stay out of
SAFE_AREA: dict[str, tuple[float, float]] = {
    "vertical": (0.10, 0.22),
    "portrait": (0.07, 0.14),
    "square": (0.07, 0.12),
    "landscape": (0.08, 0.12),
}
_DEFAULT_PRIMARY = "#1e293b"
_DEFAULT_SECONDARY = "#0f172a"
_DEFAULT_ACCENT = "#38bdf8"


@dataclass(frozen=True)
class BrandLook:
    primary_hex: str = ""
    secondary_hex: str = ""
    accent_hex: str = ""
    heading_font: Optional[str] = None


def _shade(rgb: tuple[int, int, int], factor: float) -> np.ndarray:
    return np.array(rgb, dtype=np.float32) * factor


def gradient_background(size: tuple[int, int], look: BrandLook) -> Image.Image:
    """A soft diagonal gradient between two dark shades of the brand's colours, with a faint glow of the accent colour
    and a gentle vignette. Looks deliberate even when the brand has set no colours."""
    w, h = size
    primary = _rgb(_hex_or_default(look.primary_hex, _DEFAULT_PRIMARY))
    secondary = _rgb(_hex_or_default(look.secondary_hex, _DEFAULT_SECONDARY))
    accent = _rgb(_hex_or_default(look.accent_hex, _DEFAULT_ACCENT))
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    t = ((xs / w) + (ys / h)) / 2.0
    start, end = _shade(secondary, 0.9), _shade(primary, 0.55)
    img = start[None, None, :] * (1 - t[..., None]) + end[None, None, :] * t[..., None]
    glow = np.exp(-(((xs - 0.25 * w) ** 2) + ((ys - 0.30 * h) ** 2)) / (2 * (0.45 * w) ** 2))
    img += np.array(accent, dtype=np.float32)[None, None, :] * (0.20 * glow)[..., None]
    r = np.sqrt(((xs - w / 2) / (w / 2)) ** 2 + ((ys - h / 2) / (h / 2)) ** 2) / 1.4142
    img *= (1.0 - 0.35 * np.clip(r, 0, 1) ** 2)[..., None]
    return Image.fromarray(np.clip(img, 0, 255).astype("uint8"), "RGB")


def _cover_fill(cover: Image.Image, size: tuple[int, int]) -> Image.Image:
    w, h = size
    scale = max(w / cover.width, h / cover.height)
    resized = cover.resize((max(1, int(cover.width * scale)), max(1, int(cover.height * scale))), Image.LANCZOS)
    left, top = (resized.width - w) // 2, (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (img.width - 1, img.height - 1)], radius=radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def cover_background(cover_bytes: bytes, size: tuple[int, int], size_name: str) -> Image.Image:
    """The brand's cover. Fills a square frame; in a taller or wider frame it sits sharp in the middle over a blurred,
    darkened copy of itself, so nothing is stretched or cropped awkwardly."""
    w, h = size
    cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
    if size_name == "square" or abs((cover.width / cover.height) - (w / h)) < 0.08:
        return _cover_fill(cover, size)
    back = _cover_fill(cover, size).filter(ImageFilter.GaussianBlur(radius=max(12, w // 40)))
    back = Image.eval(back, lambda v: int(v * 0.5))
    if size_name == "landscape":
        box_w = box_h = int(h * 0.74)
        cx, cy = w // 2, h // 2
    else:
        box_w = int(w * 0.84)
        box_h = int(h * 0.50)
        cx, cy = w // 2, int(h * 0.40)
    fit = min(box_w / cover.width, box_h / cover.height)
    front = _rounded(cover.resize((max(1, int(cover.width * fit)), max(1, int(cover.height * fit))), Image.LANCZOS), radius=max(12, w // 36))
    back = back.convert("RGBA")
    back.alpha_composite(front, (cx - front.width // 2, cy - front.height // 2))
    return back.convert("RGB")


def _bottom_scrim(img: Image.Image, strength: float) -> Image.Image:
    """Darkens the lower part gradually so captions are readable over any picture."""
    w, h = img.size
    start = int(h * 0.55)
    ramp = np.zeros((h, 1), dtype=np.float32)
    span = h - start
    ramp[start:, 0] = np.linspace(0.0, strength, span) ** 1.3
    arr = np.asarray(img, dtype=np.float32) * (1.0 - ramp[..., None])
    return Image.fromarray(arr.astype("uint8"), "RGB")


def compose_layers(
    *,
    size: tuple[int, int],
    size_name: str,
    style: str,
    look: BrandLook,
    cover_bytes: Optional[bytes] = None,
    title: str = "",
    logo_bytes: Optional[bytes] = None,
) -> tuple[bytes, Optional[bytes]]:
    """(background PNG, text layer PNG or None). The background is the brand gradient (or the cover over a blurred fill)
    with a darkened lower area; the text layer is a transparent picture holding the logo and title. They are kept apart so
    the background can drift slowly while the title and logo stay still. Never raises: a cover that cannot be read falls
    back to the gradient."""
    w, h = size
    img: Optional[Image.Image] = None
    if cover_bytes and style in ("cover", "cover_wave"):
        try:
            img = _bottom_scrim(cover_background(cover_bytes, size, size_name), 0.62)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cover could not be used for the video background, using the brand gradient: %s", exc)
    if img is None:
        img = _bottom_scrim(gradient_background(size, look), 0.35)
    background = io.BytesIO()
    img.save(background, format="PNG")

    safe_top, _ = SAFE_AREA.get(size_name, (0.08, 0.12))
    margin = int(w * 0.08)
    y = int(h * safe_top)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    drew = False

    if logo_bytes:
        try:
            logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            logo.thumbnail((int(w * 0.26), int(min(w, h) * 0.085)), Image.LANCZOS)
            layer.alpha_composite(logo, (margin, y))
            y += logo.height + int(h * 0.02)
            drew = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Logo could not be placed on the video, skipping: %s", exc)

    title = (title or "").strip()
    if title:
        draw = ImageDraw.Draw(layer)
        size_px = max(28, w // 20)
        font = _load_font(look.heading_font, size_px, bold=True)
        for line in _wrap_text(draw, title, font, w - margin * 2)[:2]:
            draw.text((margin + 2, y + 2), line, font=font, fill=(0, 0, 0, 120))
            draw.text((margin, y), line, font=font, fill=(255, 255, 255, 255))
            y += int(size_px * 1.3)
        drew = True

    text_layer = None
    if drew:
        out = io.BytesIO()
        layer.save(out, format="PNG")
        text_layer = out.getvalue()
    return background.getvalue(), text_layer


def compose_base_frame(**kwargs) -> bytes:
    """The two layers flattened into one picture (a still preview, and the form the tests use)."""
    background, text_layer = compose_layers(**kwargs)
    base = Image.open(io.BytesIO(background)).convert("RGBA")
    if text_layer:
        base.alpha_composite(Image.open(io.BytesIO(text_layer)).convert("RGBA"))
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue()
