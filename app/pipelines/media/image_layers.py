"""Layer based picture drawing, the engine behind the image editor.

A picture is a clean background (usually the AI picture, with no words on it) plus a list of layers: text, shapes, the
brand's logo and mascot, icons and uploaded images. The editor in the browser moves and edits those layers; this module
draws exactly the same layers into the final PNG on the server, so what is saved is what the editor showed. Because the
background is kept clean and separate, making a new AI picture never wipes the member's text and graphics.

Positions are fractions of the canvas (0 to 1), so the same design works at any size. Font sizes are a fraction of the
canvas width. Text boxes grow with their text (their height is not stored); other layers use their own box.

`default_layers` builds the starting design (soft brand band, accent bar, headline, logo) so every flow that makes a
picture produces something the member can edit."""
from __future__ import annotations

import io
import logging
import re
from typing import Optional
from uuid import uuid4

from PIL import Image, ImageDraw

from app.models.image_asset import Layer
from app.pipelines.media.icons import DEFAULT_ACCENT_ICON, icon_char, is_known_icon, load_icon_font
from app.pipelines.media.image_render import (
    BrandTokens,
    _MARGIN_FRACTION,
    _brand_band_color,
    _fit_background,
    _fit_headline,
    _hex_or_default,
    _load_font,
    _rgb,
    _wrap_text,
)

logger = logging.getLogger(__name__)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_LAYERS = 60


def _color(value: Optional[str], fallback: str) -> tuple[int, int, int]:
    return _rgb(value if value and _HEX.match(value) else fallback)


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# drawing one layer onto its own transparent tile
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _text_tile(layer: Layer, canvas_w: int, brand: BrandTokens) -> Image.Image:
    box_w = max(8, int(layer.w * canvas_w))
    size_px = max(8, int(layer.size * canvas_w))
    font = _load_font(layer.font or brand.heading_font, size_px, bold=layer.bold)
    pad = int(size_px * 0.45) if layer.box_color else 0
    inner_w = max(8, box_w - 2 * pad)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    text = layer.text.upper() if layer.uppercase else layer.text
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(_wrap_text(probe, paragraph, font, inner_w) or [""])
    line_h = int(size_px * layer.line_height)
    block_h = max(line_h, len(lines) * line_h)
    tile = Image.new("RGBA", (box_w, block_h + 2 * pad), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    if layer.box_color:
        draw.rounded_rectangle([(0, 0), (box_w - 1, tile.height - 1)], radius=int(size_px * 0.35), fill=(*_color(layer.box_color, "#000000"), 255))
    main = (*_color(layer.color, "#FFFFFF"), 255)
    accent = (*_color(layer.accent_color, brand.accent_hex), 255)
    spacing = layer.letter_spacing * size_px
    y = pad
    for line in lines:
        width = _line_width(probe, line, font, spacing)
        x = pad + {"left": 0, "center": (inner_w - width) / 2, "right": inner_w - width}[layer.align]
        if layer.shadow:
            _draw_line(draw, (x + size_px * 0.04, y + size_px * 0.05), line, font, (0, 0, 0, 120), spacing, None, None)
        _draw_line(draw, (x, y), line, font, main, spacing, layer.accent_word.strip() or None, accent)
        y += line_h
    return tile


def _line_width(probe: ImageDraw.ImageDraw, line: str, font, spacing: float) -> float:
    if not line:
        return 0.0
    if not spacing:
        box = probe.textbbox((0, 0), line, font=font)
        return float(box[2] - box[0])
    return sum(probe.textlength(ch, font=font) + spacing for ch in line) - spacing


def _draw_line(draw: ImageDraw.ImageDraw, origin: tuple[float, float], line: str, font, fill, spacing: float, accent_word: Optional[str], accent_fill) -> None:
    """One line of text. With letter spacing each letter is placed by hand; the accent word, when it is on this line, is
    drawn in the accent colour (the first match only, like the highlight in the finished picture)."""
    lo = line.lower()
    a_start = lo.find(accent_word.lower()) if accent_word else -1
    a_end = a_start + len(accent_word) if a_start >= 0 and accent_word else -1
    x, y = origin
    if not spacing and a_start < 0:
        draw.text((x, y), line, font=font, fill=fill)
        return
    for i, ch in enumerate(line):
        colour = accent_fill if a_start <= i < a_end else fill
        draw.text((x, y), ch, font=font, fill=colour)
        x += draw.textlength(ch, font=font) + spacing


def _shape_tile(layer: Layer, canvas_w: int, canvas_h: int) -> Image.Image:
    w, h = max(1, int(layer.w * canvas_w)), max(1, int(layer.h * canvas_h))
    fill = _color(layer.fill, "#000000")
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if layer.shape in ("gradient_up", "gradient_down"):
        # fades from nothing to the fill colour: "up" is solid at the bottom (a band under text), "down" solid at the top
        draw = ImageDraw.Draw(tile)
        for row in range(h):
            progress = row / max(1, h - 1)
            progress = progress if layer.shape == "gradient_up" else 1 - progress
            draw.line([(0, row), (w, row)], fill=(*fill, int(255 * min(1.0, progress * 1.6) ** 1.2)))
        return tile
    draw = ImageDraw.Draw(tile)
    stroke_px = int(layer.stroke_w * canvas_w)
    outline = (*_color(layer.stroke, "#FFFFFF"), 255) if layer.stroke and stroke_px > 0 else None
    box = [(0, 0), (w - 1, h - 1)]
    if layer.shape == "ellipse":
        draw.ellipse(box, fill=(*fill, 255), outline=outline, width=stroke_px or 1)
    else:
        radius = int(min(w, h) * layer.radius)
        draw.rounded_rectangle(box, radius=radius, fill=(*fill, 255), outline=outline, width=stroke_px or 1)
    return tile


def _image_tile(layer: Layer, canvas_w: int, canvas_h: int, data: Optional[bytes]) -> Optional[Image.Image]:
    if not data:
        return None
    try:
        picture = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Layer %s: picture could not be read, skipping: %s", layer.id, exc)
        return None
    w, h = max(1, int(layer.w * canvas_w)), max(1, int(layer.h * canvas_h))
    scale = (max if layer.fit == "cover" else min)(w / picture.width, h / picture.height)
    picture = picture.resize((max(1, int(picture.width * scale)), max(1, int(picture.height * scale))), Image.LANCZOS)
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    _paste_clipped(tile, picture, (w - picture.width) // 2, (h - picture.height) // 2)
    return tile


def _icon_tile(layer: Layer, canvas_w: int, canvas_h: int) -> Optional[Image.Image]:
    if not is_known_icon(layer.icon):
        return None
    w, h = max(1, int(layer.w * canvas_w)), max(1, int(layer.h * canvas_h))
    size = max(8, min(w, h))
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    font = load_icon_font(size)
    glyph = icon_char(layer.icon)  # type: ignore[arg-type]
    draw = ImageDraw.Draw(tile)
    box = draw.textbbox((0, 0), glyph, font=font)
    draw.text(((w - (box[2] - box[0])) / 2 - box[0], (h - (box[3] - box[1])) / 2 - box[1]), glyph, font=font, fill=(*_color(layer.color, "#FFFFFF"), 255))
    return tile


def _tile_for(layer: Layer, canvas_w: int, canvas_h: int, brand: BrandTokens, assets: dict[str, bytes]) -> Optional[Image.Image]:
    if layer.type == "text":
        return _text_tile(layer, canvas_w, brand) if layer.text.strip() else None
    if layer.type == "shape":
        return _shape_tile(layer, canvas_w, canvas_h)
    if layer.type in ("image", "logo", "mascot"):
        return _image_tile(layer, canvas_w, canvas_h, assets.get(layer.id))
    if layer.type == "icon":
        return _icon_tile(layer, canvas_w, canvas_h)
    return None


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# the whole picture
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def render_layers(
    *,
    size: tuple[int, int],
    background_bytes: Optional[bytes],
    layers: list[Layer],
    brand: BrandTokens,
    assets: Optional[dict[str, bytes]] = None,
) -> bytes:
    """The finished PNG. `assets` maps a layer id to the bytes of its picture (logo, mascot or uploaded image); a layer
    whose picture is missing is skipped, never drawn as a placeholder."""
    assets = assets or {}
    width, height = size
    canvas: Image.Image
    if background_bytes:
        try:
            canvas = _fit_background(background_bytes, size).convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Background could not be read, using the brand colour: %s", exc)
            canvas = Image.new("RGBA", size, (*_color(brand.primary_hex, "#16161D"), 255))
    else:
        canvas = Image.new("RGBA", size, (*_color(brand.primary_hex, "#16161D"), 255))

    for layer in layers[:MAX_LAYERS]:
        if layer.hidden:
            continue
        tile = _tile_for(layer, width, height, brand, assets)
        if tile is None:
            continue
        if layer.opacity < 1.0:
            alpha = tile.getchannel("A").point(lambda v, o=layer.opacity: int(v * o))
            tile.putalpha(alpha)
        cx = layer.x * width + tile.width / 2
        cy = layer.y * height + tile.height / 2
        if layer.rotation:
            tile = tile.rotate(-layer.rotation, expand=True, resample=Image.BICUBIC)
        _paste_clipped(canvas, tile, int(round(cx - tile.width / 2)), int(round(cy - tile.height / 2)))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _paste_clipped(canvas: Image.Image, tile: Image.Image, x: int, y: int) -> None:
    """Pastes a tile that may hang over the edge of the canvas (a layer dragged partly off the picture)."""
    left, top = max(0, -x), max(0, -y)
    right = min(tile.width, canvas.width - x)
    bottom = min(tile.height, canvas.height - y)
    if right <= left or bottom <= top:
        return
    canvas.alpha_composite(tile.crop((left, top, right, bottom)), (max(0, x), max(0, y)))


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# the starting design
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _new_id() -> str:
    return uuid4().hex[:10]


def default_layers(
    *,
    size: tuple[int, int],
    brand: BrandTokens,
    headline: str,
    show_text: bool = True,
    accent_keyword: str = "",
    author: Optional[str] = None,
    has_logo: bool = False,
    has_mascot: bool = False,
    icon_name: Optional[str] = None,
    illustration_accent: bool = False,
) -> list[Layer]:
    """The design a new picture starts with, bottom layer first: an optional large faint icon, a soft brand band, an accent
    bar, the headline, a small icon, the author line, the logo and the mascot. Sized with the same fitting rules the old
    one-piece renderer used, so a headline never runs off the picture."""
    width, height = size
    margin = _MARGIN_FRACTION
    accent = _hex_or_default(brand.accent_hex, "#38bdf8")
    layers: list[Layer] = []

    if illustration_accent:
        art = DEFAULT_ACCENT_ICON if not is_known_icon(icon_name) else icon_name
        layers.append(Layer(id=_new_id(), type="icon", name="Background icon", icon=art, x=0.46, y=0.02, w=0.62, h=0.62 * width / height,
                            color=accent, opacity=0.18))

    headline = (headline or "").strip()
    if show_text and headline:
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        font, lines, size_px = _fit_headline(probe, headline, brand.heading_font, int(width * (1 - 2 * margin)), size)
        line_h = int(size_px * 1.3)
        block_h = len(lines) * line_h
        icon = icon_name if is_known_icon(icon_name) else None
        icon_px = int(min(size) * 0.12) if icon else 0
        bar_h = max(4, height // 200)
        gap = int(min(size) * margin) // 3
        text_top = height - int(min(size) * margin) - block_h
        stack_top = text_top - (icon_px + gap if icon else bar_h + gap)
        band_top = max(0, stack_top - int(min(size) * margin) // 2)
        fade = int(min(size) * margin) * 2
        top = max(0, band_top - fade)
        layers.append(Layer(id=_new_id(), type="shape", name="Text band", shape="gradient_up", fill=_hex(_brand_band_color(brand)),
                            x=0.0, y=top / height, w=1.0, h=(height - top) / height, opacity=0.85))
        if icon:
            layers.append(Layer(id=_new_id(), type="icon", name="Icon", icon=icon, x=margin, y=stack_top / height, w=icon_px / width, h=icon_px / height, color=accent))
        else:
            layers.append(Layer(id=_new_id(), type="shape", name="Accent bar", shape="rect", fill=accent, x=margin, y=(text_top - gap - bar_h) / height,
                                w=0.09, h=bar_h / height))
        layers.append(Layer(id=_new_id(), type="text", name="Headline", text=headline, x=margin, y=text_top / height, w=1 - 2 * margin,
                            size=size_px / width, font=brand.heading_font, bold=True, color="#FFFFFF", align="left", line_height=1.3,
                            accent_word=accent_keyword.strip(), accent_color=accent))
    if author and author.strip():
        layers.append(Layer(id=_new_id(), type="text", name="Author", text=author.strip(), x=margin, y=margin / 2, w=1 - 2 * margin,
                            size=max(16, width // 48) / width, font=brand.body_font, bold=False, color="#FFFFFF", shadow=True))
    if has_logo:
        edge = 0.08 * min(size)
        layers.append(Layer(id=_new_id(), type="logo", name="Logo", x=margin, y=margin, w=edge / width, h=edge / height, fit="contain"))
    if has_mascot:
        edge = 0.2 * min(size)
        layers.append(Layer(id=_new_id(), type="mascot", name="Mascot", x=1 - margin / 2 - edge / width, y=margin / 2, w=edge / width, h=edge / height, fit="contain"))
    return layers


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def layer_assets_needed(layers: list[Layer]) -> list[Layer]:
    """The layers that need a picture fetched before drawing."""
    return [layer for layer in layers if layer.type in ("logo", "mascot", "image") and not layer.hidden]
