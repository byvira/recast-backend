"""Looks a member can switch on for a picture: a soft glow in the corner, a two-tone colour wash and a dotted grid. They are drawn on
the picture itself (under the headline and logo), so they are in the saved file, the download and every later edit, not only on
the screen."""
from __future__ import annotations

from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter

EFFECT_KEYS = ("glow", "dual_tone", "grid")


def wanted(effects: Optional[dict]) -> bool:
    return bool(effects) and any(bool(effects.get(k)) for k in EFFECT_KEYS)


def _rgb(hex_value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        value = (hex_value or "").lstrip("#")
        if len(value) == 3:
            value = "".join(c * 2 for c in value)
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except (ValueError, IndexError):
        return fallback


def apply_effects(canvas: Image.Image, *, primary_hex: str, accent_hex: str, effects: Optional[dict]) -> Image.Image:
    """`canvas` (RGBA) with the chosen looks drawn on it. Returns it untouched when none is chosen."""
    if not wanted(effects):
        return canvas
    width, height = canvas.size
    primary = _rgb(primary_hex, (224, 74, 31))
    accent = _rgb(accent_hex, (56, 189, 248))
    out = canvas.convert("RGBA")

    if effects.get("dual_tone"):
        # a diagonal wash from the brand colour to the accent colour
        gradient = Image.linear_gradient("L").resize((width, height))
        gradient = gradient.rotate(-35, expand=False, resample=Image.BICUBIC, fillcolor=0)
        start = Image.new("RGBA", (width, height), (*primary, 120))
        end = Image.new("RGBA", (width, height), (*accent, 95))
        wash = Image.composite(end, start, gradient)
        out = Image.alpha_composite(out, wash)

    if effects.get("grid"):
        spacing = max(14, min(width, height) // 45)
        dots = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(dots)
        radius = max(1, spacing // 12)
        for y in range(spacing // 2, height, spacing):
            for x in range(spacing // 2, width, spacing):
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 255, 255, 46))
        out = Image.alpha_composite(out, dots)

    if effects.get("glow"):
        # a soft pool of the accent colour in the top-right corner
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(glow)
        size = int(min(width, height) * 0.55)
        cx, cy = int(width * 0.88), int(height * 0.12)
        draw.ellipse((cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2), fill=(*accent, 150))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=max(12, size // 3)))
        out = Image.alpha_composite(out, glow)

    return out
