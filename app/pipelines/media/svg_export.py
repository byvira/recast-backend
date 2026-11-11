"""A picture's layers written as an SVG file a designer can open and keep editing.

Words stay words (one text element per line, laid out with the same wrapping as the finished picture), shapes stay shapes, bar charts
stay bars and icons become real outlines taken from the icon font, so all of them stay sharp at any size. Pictures (the logo, the
mascot, uploaded images) and the clean background, with the picture's looks switched on in Looks, are embedded as images. Outline, glow,
shadow and softening are written as SVG filters. Fonts are named, not embedded: a program that does not have a font draws a similar one.

It follows the same rules as `render_layers`, so what opens in a design program is what the editor showed."""
from __future__ import annotations

import base64
import io
import logging
import os
import re
from functools import lru_cache
from typing import Optional
from xml.sax.saxutils import escape, quoteattr

from PIL import Image, ImageDraw

from app.models.image_asset import MAX_CHART_BARS, Layer
from app.shared.heavy_work import stop_point
from app.pipelines.media.icons import icon_char, is_known_icon
from app.pipelines.media.image_layers import MAX_LAYERS, _color, _value_text
from app.pipelines.media.image_render import BrandTokens, _fit_background, _load_font, _wrap_text

logger = logging.getLogger(__name__)

_ICON_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "lucide.ttf")
_NOT_WORD = re.compile(r"\W")


def _hex(value: Optional[str], fallback: str) -> str:
    return "#{:02X}{:02X}{:02X}".format(*_color(value, fallback))


def _num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def _data_uri(data: bytes) -> str:
    try:
        kind = (Image.open(io.BytesIO(data)).format or "PNG").lower()
    except Exception:  # noqa: BLE001
        kind = "png"
    mime = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(kind, "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


@lru_cache(maxsize=1)
def _icon_font():
    from fontTools.ttLib import TTFont

    font = TTFont(_ICON_FONT_PATH)
    return font, font.getGlyphSet(), font.getBestCmap(), int(font["head"].unitsPerEm)


def _icon_path(name: str, width: float, height: float) -> Optional[tuple[str, str]]:
    """The outline of an icon, scaled and centred in a `width` x `height` box: (path data, transform)."""
    try:
        from fontTools.pens.boundsPen import BoundsPen
        from fontTools.pens.svgPathPen import SVGPathPen

        _, glyphs, cmap, upem = _icon_font()
        glyph_name = cmap.get(ord(icon_char(name)))
        if not glyph_name:
            return None
        bounds = BoundsPen(glyphs)
        glyphs[glyph_name].draw(bounds)
        if not bounds.bounds:
            return None
        xmin, ymin, xmax, ymax = bounds.bounds
        pen = SVGPathPen(glyphs)
        glyphs[glyph_name].draw(pen)
        size = max(8.0, min(width, height))
        scale = size / upem
        tx = (width - (xmax - xmin) * scale) / 2 - xmin * scale
        ty = (height - (ymax - ymin) * scale) / 2 + ymax * scale
        return pen.getCommands(), f"translate({_num(tx)} {_num(ty)}) scale({_num(scale)} {_num(-scale)})"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Icon %s could not be written as an outline: %s", name, exc)
        return None


def _filter(layer: Layer, canvas_w: int, safe: str) -> str:
    """The SVG filter for a layer's looks (softening, outline, glow, shadow), or an empty string when it has none."""
    outline = layer.outline_w * canvas_w if layer.outline and layer.outline_w > 0 else 0.0
    glow = layer.glow_r * canvas_w if layer.glow and layer.glow_r > 0 else 0.0
    blur = layer.blur * canvas_w
    shadow = bool(layer.shadow) and layer.type != "text"
    if not (outline or glow or blur >= 0.4 or shadow):
        return ""
    parts = [f'<filter id="fx-{safe}" x="-60%" y="-60%" width="220%" height="220%" color-interpolation-filters="sRGB">']
    if blur >= 0.4:
        parts.append(f'<feGaussianBlur in="SourceGraphic" stdDeviation="{_num(blur)}" result="base"/>')
    else:
        parts.append('<feOffset in="SourceGraphic" dx="0" dy="0" result="base"/>')
    merge: list[str] = []
    shape = "base"
    if outline:
        parts.append(f'<feMorphology in="base" operator="dilate" radius="{_num(outline)}" result="grown"/>')
        parts.append(f'<feFlood flood-color="{_hex(layer.outline, "#000000")}" result="line-colour"/>')
        parts.append('<feComposite in="line-colour" in2="grown" operator="in" result="line"/>')
        shape = "grown"
    if glow:
        parts.append(f'<feGaussianBlur in="{shape}" stdDeviation="{_num(glow)}" result="halo"/>')
        parts.append(f'<feFlood flood-color="{_hex(layer.glow, "#FFFFFF")}" result="halo-colour"/>')
        parts.append('<feComposite in="halo-colour" in2="halo" operator="in" result="light"/>')
        merge += ["light", "light"]
    if shadow:
        offset = _num(max(2.0, 0.01 * canvas_w))
        parts.append(f'<feGaussianBlur in="base" stdDeviation="{_num(max(2.0, 0.008 * canvas_w))}" result="soft"/>')
        parts.append(f'<feOffset in="soft" dx="{offset}" dy="{offset}" result="moved"/>')
        parts.append('<feFlood flood-color="#000000" flood-opacity="0.45" result="shade-colour"/>')
        parts.append('<feComposite in="shade-colour" in2="moved" operator="in" result="shade"/>')
        merge.append("shade")
    if outline:
        merge.append("line")
    merge.append("base")
    parts.append("<feMerge>" + "".join(f'<feMergeNode in="{name}"/>' for name in merge) + "</feMerge>")
    parts.append("</filter>")
    return "".join(parts)


def _text_group(layer: Layer, canvas_w: int, brand: BrandTokens) -> tuple[str, float, float]:
    """The words of a text layer, one element per line, and the size of its box."""
    box_w = max(8, int(layer.w * canvas_w))
    size_px = max(8, int(layer.size * canvas_w))
    font = _load_font(layer.font or brand.heading_font, size_px, bold=layer.bold, text=layer.text)
    pad = int(size_px * 0.45) if layer.box_color else 0
    inner_w = max(8, box_w - 2 * pad)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    text = layer.text.upper() if layer.uppercase else layer.text
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(_wrap_text(probe, paragraph, font, inner_w) or [""])
    line_h = int(size_px * layer.line_height)
    block_h = max(line_h, len(lines) * line_h)
    ascent = font.getmetrics()[0] if hasattr(font, "getmetrics") else size_px
    family = layer.font or brand.heading_font or "Inter"
    main = _hex(layer.color, "#FFFFFF")
    accent = _hex(layer.accent_color, brand.accent_hex)
    anchor, x = {"left": ("start", pad), "center": ("middle", pad + inner_w / 2), "right": ("end", pad + inner_w)}[layer.align]
    out: list[str] = []
    if layer.box_color:
        out.append(f'<rect x="0" y="0" width="{box_w}" height="{block_h + 2 * pad}" rx="{_num(size_px * 0.35)}" fill="{_hex(layer.box_color, "#000000")}"/>')
    word = layer.accent_word.strip().lower()
    for i, line in enumerate(lines):
        y = pad + i * line_h + ascent
        spans = escape(line)
        at = line.lower().find(word) if word else -1
        if at >= 0:
            spans = f'{escape(line[:at])}<tspan fill="{accent}">{escape(line[at:at + len(word)])}</tspan>{escape(line[at + len(word):])}'
        shadow = ""
        if layer.shadow:
            shadow = (f'<text x="{_num(x + size_px * 0.04)}" y="{_num(y + size_px * 0.05)}" fill="#000000" fill-opacity="0.47" '
                      f'text-anchor="{anchor}" xml:space="preserve">{escape(line)}</text>')
        out.append(f'{shadow}<text x="{_num(x)}" y="{_num(y)}" text-anchor="{anchor}" xml:space="preserve">{spans}</text>')
    letter = f' letter-spacing="{_num(layer.letter_spacing * size_px)}"' if layer.letter_spacing else ""
    body = (f'<g font-family={quoteattr(family + ", Inter, sans-serif")} font-size="{size_px}" font-weight="{700 if layer.bold else 400}" '
            f'fill="{main}"{letter}>' + "".join(out) + "</g>")
    return body, float(box_w), float(block_h + 2 * pad)


def _chart_group(layer: Layer, canvas_w: int, w: float, h: float, brand: BrandTokens) -> str:
    bars = layer.bars[:MAX_CHART_BARS]
    if not bars:
        return ""
    text_px = max(10, min(int(layer.size * canvas_w), int(h // 6)))
    ink = _hex(layer.color, "#FFFFFF")
    fill = _hex(layer.fill, brand.accent_hex)
    top_room = bottom_room = text_px * 1.6
    plot_h = max(10.0, h - top_room - bottom_room)
    base = top_room + plot_h
    slot = w / len(bars)
    bar_w = slot * 0.62
    peak = max((b.value for b in bars), default=0.0) or 1.0
    family = layer.font or brand.body_font or "Inter"
    out = [f'<line x1="0" y1="{_num(base)}" x2="{_num(w)}" y2="{_num(base)}" stroke="{ink}" stroke-opacity="0.43" stroke-width="{max(1, text_px // 10)}"/>']
    for i, bar in enumerate(bars):
        height = max(2.0, plot_h * (bar.value / peak))
        left = i * slot + (slot - bar_w) / 2
        out.append(f'<rect x="{_num(left)}" y="{_num(base - height)}" width="{_num(bar_w)}" height="{_num(height)}" '
                   f'rx="{_num(min(bar_w * 0.18, 12))}" fill="{fill}"/>')
        centre = i * slot + slot / 2
        out.append(f'<text x="{_num(centre)}" y="{_num(base - height - text_px * 0.35)}" text-anchor="middle">{escape(_value_text(bar.value))}</text>')
        out.append(f'<text x="{_num(centre)}" y="{_num(base + text_px * 1.05)}" text-anchor="middle">{escape(bar.label)}</text>')
    return (f'<g font-family={quoteattr(family + ", Inter, sans-serif")} font-size="{text_px}" font-weight="{700 if layer.bold else 400}" '
            f'fill="{ink}">' + "".join(out) + "</g>")


def _shape_group(layer: Layer, canvas_w: int, w: float, h: float, safe: str, defs: list[str]) -> str:
    fill = _hex(layer.fill, "#000000")
    if layer.shape in ("gradient_up", "gradient_down"):
        start, end = (0, 1) if layer.shape == "gradient_up" else (1, 0)
        defs.append(f'<linearGradient id="gr-{safe}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{fill}" stop-opacity="{start}"/>'
                    f'<stop offset="1" stop-color="{fill}" stop-opacity="{end}"/></linearGradient>')
        return f'<rect width="{_num(w)}" height="{_num(h)}" fill="url(#gr-{safe})"/>'
    stroke = ""
    if layer.stroke and layer.stroke_w > 0:
        stroke = f' stroke="{_hex(layer.stroke, "#FFFFFF")}" stroke-width="{_num(layer.stroke_w * canvas_w)}"'
    if layer.shape == "ellipse":
        return f'<ellipse cx="{_num(w / 2)}" cy="{_num(h / 2)}" rx="{_num(w / 2)}" ry="{_num(h / 2)}" fill="{fill}"{stroke}/>'
    return f'<rect width="{_num(w)}" height="{_num(h)}" rx="{_num(min(w, h) * layer.radius)}" fill="{fill}"{stroke}/>'


def _picture_group(layer: Layer, w: float, h: float, data: bytes, safe: str, defs: list[str]) -> str:
    fit = "xMidYMid slice" if layer.fit == "cover" else "xMidYMid meet"
    clip = ""
    if layer.radius > 0 or layer.fit == "cover":
        defs.append(f'<clipPath id="cp-{safe}"><rect width="{_num(w)}" height="{_num(h)}" rx="{_num(min(w, h) * min(0.5, layer.radius))}"/></clipPath>')
        clip = f' clip-path="url(#cp-{safe})"'
    return f'<image width="{_num(w)}" height="{_num(h)}" preserveAspectRatio="{fit}" href="{_data_uri(data)}"{clip}/>'


def build_svg(
    *, size: tuple[int, int], background_bytes: Optional[bytes], layers: list[Layer], brand: BrandTokens,
    assets: Optional[dict[str, bytes]] = None, effects: Optional[dict] = None, place: tuple[float, float] = (0.5, 0.5),
) -> str:
    """The picture as SVG text. The background (with the picture's own looks) is one embedded image under the layers, and rounded
    corners are a clip over everything; a layer whose picture is missing is left out, as in the finished picture."""
    from app.pipelines.media.image_effects import apply_effects

    assets = assets or {}
    width, height = size
    if background_bytes:
        try:
            canvas = _fit_background(background_bytes, size, place).convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Background could not be read for SVG, using the brand colour: %s", exc)
            canvas = Image.new("RGBA", size, (*_color(brand.primary_hex, "#1C1814"), 255))
    else:
        canvas = Image.new("RGBA", size, (*_color(brand.primary_hex, "#1C1814"), 255))
    canvas = apply_effects(canvas, primary_hex=brand.primary_hex, accent_hex=brand.accent_hex, effects=effects)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")

    defs: list[str] = []
    body: list[str] = [f'<image width="{width}" height="{height}" href="{_data_uri(buf.getvalue())}"/>']
    for layer in layers[:MAX_LAYERS]:
        stop_point()
        if layer.hidden:
            continue
        safe = _NOT_WORD.sub("_", layer.id)
        ox, oy = layer.x * width, layer.y * height
        w, h = layer.w * width, layer.h * height
        inner = ""
        if layer.type == "text":
            if not layer.text.strip():
                continue
            inner, w, h = _text_group(layer, width, brand)
        elif layer.type == "shape":
            inner = _shape_group(layer, width, max(1.0, w), max(1.0, h), safe, defs)
        elif layer.type == "chart":
            w, h = max(40.0, w), max(40.0, h)
            inner = _chart_group(layer, width, w, h, brand)
        elif layer.type == "icon":
            if not is_known_icon(layer.icon):
                continue
            made = _icon_path(layer.icon or "", w, h)
            if not made:
                continue
            inner = f'<path d={quoteattr(made[0])} transform="{made[1]}" fill="{_hex(layer.color, "#FFFFFF")}"/>'
        elif layer.type in ("image", "logo", "mascot"):
            data = assets.get(layer.id)
            if not data:
                continue
            inner = _picture_group(layer, max(1.0, w), max(1.0, h), data, safe, defs)
        if not inner:
            continue
        flt = _filter(layer, width, safe)
        if flt:
            defs.append(flt)
        move = f"translate({_num(ox)} {_num(oy)})"
        if layer.rotation:
            move += f" rotate({_num(layer.rotation)} {_num(w / 2)} {_num(h / 2)})"
        attrs = [f'transform="{move}"']
        if layer.opacity < 1.0:
            attrs.append(f'opacity="{_num(layer.opacity)}"')
        if flt:
            attrs.append(f'filter="url(#fx-{safe})"')
        body.append(f'<g id="layer-{safe}" data-name={quoteattr(layer.name or layer.type)} {" ".join(attrs)}>{inner}</g>')

    corner = min(0.25, float((effects or {}).get("corner_radius") or 0.0))
    clip_def, open_wrap, close_wrap = "", "", ""
    if corner > 0:
        clip_def = f'<clipPath id="picture-corners"><rect width="{width}" height="{height}" rx="{_num(min(width, height) * corner)}"/></clipPath>'
        open_wrap, close_wrap = '<g clip-path="url(#picture-corners)">', "</g>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<defs>{"".join(defs)}{clip_def}</defs>{open_wrap}{"".join(body)}{close_wrap}</svg>\n'
    )
