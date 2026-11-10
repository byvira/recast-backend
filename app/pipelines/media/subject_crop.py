"""Finding the main subject of a picture and cropping it for other platform shapes.

Nothing here calls a model or an outside service. The subject is found from the picture itself: where its edges are strongest and where its
colours stand furthest from the picture's average, weighted a little towards the middle (the spot the eye goes first). Only the strongest places count, so a small bright
subject is not pulled back to the middle by quiet background. It does not look for faces; a picture with several equally strong places
gets a middle-ground answer, which is why the member sees the crop and can move it.

A crop window is described by a placement, two numbers from 0 to 1 for how far along its free direction it sits (0 the start, 0.5 the middle,
1 the end). This is exactly what CSS calls an object position, so the editor's preview and the finished picture agree."""
from __future__ import annotations

import math
from io import BytesIO
from typing import Optional

from PIL import Image, ImageFilter, ImageStat

from app.models.image_asset import Layer

_ANALYSIS_SIZE = 96
_EDGE_BLUR = 2
_CENTRE_SPREAD = 0.35
# Only places at least this share as strong as the strongest count, and a picture whose strongest place scores below the floor is flat.
_STRONG_SHARE = 0.35
_FLAT_BELOW = 6.0


def find_focus(data: bytes) -> tuple[float, float]:
    """Where the main subject is, as fractions of the picture's width and height. (0.5, 0.5) when the picture is flat or cannot be read."""
    try:
        with Image.open(BytesIO(data)) as source:
            img = source.convert("RGB")
    except Exception:  # noqa: BLE001
        return 0.5, 0.5
    img.thumbnail((_ANALYSIS_SIZE, _ANALYSIS_SIZE))
    w, h = img.size
    if w < 4 or h < 4:
        return 0.5, 0.5
    edges = img.convert("L").filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(_EDGE_BLUR))
    mean = ImageStat.Stat(img).mean
    pixels, edge_pixels = img.load(), edges.load()
    scored: list[tuple[float, int, int]] = []
    edge_band = _EDGE_BLUR + 2
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            colour = (abs(r - mean[0]) + abs(g - mean[1]) + abs(b - mean[2])) / 3
            dx, dy = (x + 0.5) / w - 0.5, (y + 0.5) / h - 0.5
            prior = math.exp(-(dx * dx + dy * dy) / (2 * _CENTRE_SPREAD ** 2))
            # the edge filter leaves the outer rows of pixels unfiltered, which would read as a bright frame round every picture
            edge = edge_pixels[x, y] if edge_band <= x < w - edge_band and edge_band <= y < h - edge_band else 0
            scored.append(((edge + 0.6 * colour) * (0.55 + 0.45 * prior), x, y))
    peak = max(score for score, _, _ in scored)
    if peak < _FLAT_BELOW:
        return 0.5, 0.5  # nothing stands out: the picture is flat
    total = sx = sy = 0.0
    for score, x, y in scored:
        if score >= peak * _STRONG_SHARE:
            weight = (score / peak) ** 2
            total += weight
            sx += weight * (x + 0.5)
            sy += weight * (y + 0.5)
    return min(1.0, max(0.0, sx / total / w)), min(1.0, max(0.0, sy / total / h))


def placement(source: tuple[int, int], target: tuple[int, int], focus: tuple[float, float]) -> tuple[float, float]:
    """Where the crop window for `target`'s shape sits on a `source` picture so that `focus` is as near its middle as the edges allow.
    Only the side that has to be cut moves; the other stays at 0.5."""
    src_w, src_h = source
    ratio = target[0] / target[1]
    if src_w / src_h > ratio:
        crop = src_h * ratio
        free = src_w - crop
        along = (focus[0] * src_w - crop / 2) / free if free > 1 else 0.5
        return min(1.0, max(0.0, along)), 0.5
    crop = src_w / ratio
    free = src_h - crop
    along = (focus[1] * src_h - crop / 2) / free if free > 1 else 0.5
    return 0.5, min(1.0, max(0.0, along))


def adapt_layers(layers: list[Layer], source: tuple[int, int], target: tuple[int, int], *, heading_font: Optional[str], body_font: Optional[str]) -> list[Layer]:
    """The same design on a differently shaped picture. Positions stay as fractions, so a bottom headline stays at the bottom and a band
    stays a band; pictures, icons and charts keep the shape of their box in pixels instead of stretching; and words that would run off the
    bottom are made smaller (down to 60%) and then moved up until they fit."""
    from PIL import ImageDraw

    from app.pipelines.media.image_render import _load_font, _wrap_text

    src_w, src_h = source
    dst_w, dst_h = target
    scale = dst_w / src_w
    out: list[Layer] = []
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    def text_height(layer: Layer, size: float) -> float:
        px = max(8, int(size * dst_w))
        font = _load_font(layer.font or heading_font or body_font, px, bold=layer.bold, text=layer.text)
        pad = int(px * 0.45) if layer.box_color else 0
        lines: list[str] = []
        for paragraph in (layer.text.upper() if layer.uppercase else layer.text).split("\n"):
            lines.extend(_wrap_text(probe, paragraph, font, max(8, int(layer.w * dst_w) - 2 * pad)) or [""])
        return (max(int(px * layer.line_height), len(lines) * int(px * layer.line_height)) + 2 * pad) / dst_h

    for layer in layers:
        change: dict = {}
        if layer.type == "text" or (layer.type == "watermark" and layer.watermark_kind == "text"):
            size, y = layer.size, layer.y
            room = 0.96 - y
            height = text_height(layer, size)
            floor = size * 0.6
            while height > room and size > floor:
                size = max(floor, size * 0.92)
                height = text_height(layer, size)
            if y + height > 0.96:
                y = max(0.02, 0.96 - height)
            change = {"size": round(size, 4), "y": round(y, 4)}
        elif layer.type in ("image", "logo", "mascot", "icon", "chart", "watermark"):
            h = layer.h * src_h * scale / dst_h
            h = min(h, 1.0)
            change = {"h": round(max(0.001, h), 4), "y": round(min(layer.y, max(0.0, 1 - h)), 4)}
        out.append(layer.model_copy(update=change) if change else layer.model_copy())
    return out
