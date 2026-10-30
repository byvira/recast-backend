"""A brand's watermark on its pictures.

A watermark layer names which of the brand's two marks it draws (words or a picture) and always draws the brand's current one,
like the logo and mascot layers. Before drawing, `resolve_layers` turns each one into the plain text or image layer it stands for,
so the drawing code needs nothing new. `stamp` puts the brand's chosen mark on a finished picture, for exports with the
watermark switched on.
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image

from app.models.image_asset import Layer
from app.pipelines.media.image_render import BrandTokens

#: Space between the mark and the picture's edge, as a fraction of the picture's shorter side.
EDGE = 0.035
#: A text mark's letters, as a fraction of the mark's width (so a wider mark has larger letters).
TEXT_SIZE_OF_WIDTH = 0.16


def has_mark(watermark: Optional[dict], kind: str) -> bool:
    watermark = watermark or {}
    return bool((watermark.get("text") or "").strip()) if kind == "text" else bool(watermark.get("image_url"))


def resolve_layers(layers: list[Layer], watermark: Optional[dict]) -> list[Layer]:
    """The layers with every watermark layer replaced by what it draws: a text layer with the brand's words and style, or an
    image layer for the brand's picture mark (its file is fetched under the same layer id). A watermark the brand has not set
    is left out, so nothing is drawn in its place."""
    watermark = watermark or {}
    out: list[Layer] = []
    for layer in layers:
        if layer.type != "watermark":
            out.append(layer)
            continue
        if not has_mark(watermark, layer.watermark_kind):
            continue
        if layer.watermark_kind == "text":
            out.append(layer.model_copy(update={
                "type": "text", "text": watermark["text"].strip(), "font": watermark.get("text_font") or layer.font,
                "color": watermark.get("text_color") or layer.color, "bold": bool(watermark.get("text_bold", True)),
            }))
        else:
            out.append(layer.model_copy(update={"type": "image", "fit": "contain"}))
    return out


def auto_layer(watermark: dict, width: int, height: int, mark_size: Optional[tuple[int, int]] = None) -> Optional[Layer]:
    """The layer for the brand's chosen mark on a `width` x `height` picture, at its chosen corner, size and see-through level.
    `mark_size` is the picture mark's own size, so its box keeps its shape."""
    kind = watermark.get("auto_kind") or "text"
    if not has_mark(watermark, kind):
        return None
    w = float(watermark.get("size") or 0.2)
    if kind == "image":
        mw, mh = mark_size or (1, 1)
        h = w * width * (mh / max(1, mw)) / height
        size = 0.05
    else:
        size = max(0.008, w * TEXT_SIZE_OF_WIDTH)
        h = size * width * 1.4 / height
    edge_x = EDGE * min(width, height) / width
    edge_y = EDGE * min(width, height) / height
    position = watermark.get("position") or "bottom_right"
    x = {"bottom_left": edge_x, "top_left": edge_x, "center": (1 - w) / 2}.get(position, 1 - w - edge_x)
    y = {"top_left": edge_y, "top_right": edge_y, "center": (1 - h) / 2}.get(position, 1 - h - edge_y)
    if kind == "text":
        # The words' size comes from `size`; their box spans the picture inside the margins and the alignment puts them in the
        # corner, so a longer mark never wraps onto a second line.
        x, w = edge_x, 1 - 2 * edge_x
    align = "left" if position in ("bottom_left", "top_left") else "center" if position == "center" else "right"
    return Layer(
        id="watermark", type="watermark", watermark_kind=kind, name="Watermark", x=x, y=y, w=w, h=max(0.001, h),
        opacity=float(watermark.get("opacity") or 0.6), size=size, align=align, shadow=True,
    )


def stamp(picture: bytes, watermark: dict, brand: BrandTokens, mark_bytes: Optional[bytes] = None) -> bytes:
    """`picture` with the brand's chosen mark on it, as PNG. A picture with see-through parts (rounded corners) keeps them.
    Returns the picture unchanged when the chosen mark is not set."""
    from app.pipelines.media.image_layers import _paste_clipped, _tile_for

    with Image.open(io.BytesIO(picture)) as source:
        has_alpha = source.mode in ("RGBA", "LA") or (source.mode == "P" and "transparency" in source.info)
        canvas = source.convert("RGBA")
    mark_size = None
    if mark_bytes:
        with Image.open(io.BytesIO(mark_bytes)) as mark:
            mark_size = mark.size
    layer = auto_layer(watermark, canvas.width, canvas.height, mark_size)
    if layer is None or (layer.watermark_kind == "image" and not mark_bytes):
        return picture
    drawn = resolve_layers([layer], watermark)[0]
    tile = _tile_for(drawn, canvas.width, canvas.height, brand, {drawn.id: mark_bytes} if mark_bytes else {})
    if tile is None:
        return picture
    if drawn.opacity < 1.0:
        tile.putalpha(tile.getchannel("A").point(lambda v, o=drawn.opacity: int(v * o)))
    # a text mark's box is as tall as its lines, so it is placed by its real height
    y = drawn.y * canvas.height
    if drawn.type == "text":
        position = watermark.get("position") or "bottom_right"
        edge = EDGE * min(canvas.width, canvas.height)
        y = edge if position.startswith("top") else (canvas.height - tile.height) / 2 if position == "center" else canvas.height - tile.height - edge
    _paste_clipped(canvas, tile, int(round(drawn.x * canvas.width)), int(round(y)))
    out = io.BytesIO()
    (canvas if has_alpha else canvas.convert("RGB")).save(out, format="PNG")
    return out.getvalue()
