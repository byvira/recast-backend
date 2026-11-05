"""Where a brand's logo goes on a picture, and what sits behind it so it can always be seen.

A brand picks one of five spots. Top-left is the starting place, so nothing changes until a brand chooses another. A logo drawn along the
bottom is kept above the headline band, never over the words.

Behind the logo there can be a soft plate. With "auto" the picture is measured under the logo and the plate is the opposite shade: a
white plate on a dark photo, a dark plate on a light one. That works with any logo, so a brand needs no separate light and dark
files. "light" and "dark" force one shade, and "none" draws nothing."""
from __future__ import annotations

from typing import Optional

from PIL import Image, ImageDraw

POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right", "center")
BACKINGS = ("none", "auto", "light", "dark")
DEFAULT_POSITION = "top_left"
DEFAULT_BACKING = "none"

#: Above this average brightness (0 to 1) the picture under a logo counts as light, and the plate goes dark.
LIGHT_ABOVE = 0.55
_PLATE_LIGHT = (255, 255, 255, 224)
_PLATE_DARK = (20, 17, 15, 199)
#: The plate extends past the logo by this fraction of the logo's shorter side.
PLATE_PAD = 0.18


def clean_position(value: Optional[str]) -> str:
    return value if value in POSITIONS else DEFAULT_POSITION


def clean_backing(value: Optional[str]) -> str:
    return value if value in BACKINGS else DEFAULT_BACKING


def place(
    position: str, canvas: tuple[int, int], logo: tuple[int, int], margin: tuple[int, int], floor: Optional[int] = None,
) -> tuple[int, int]:
    """The top-left corner, in pixels, for a logo of this size. `floor` is the lowest line the logo's bottom edge may reach (the top of
    the headline band), so a bottom position sits above the words."""
    cw, ch = canvas
    lw, lh = logo
    mx, my = margin
    position = clean_position(position)
    if position == "center":
        return (cw - lw) // 2, (ch - lh) // 2
    x = mx if position.endswith("left") else cw - mx - lw
    if position.startswith("top"):
        return x, my
    bottom = ch - my if floor is None else min(ch - my, floor)
    return x, max(my, bottom - lh)


def brightness_under(canvas: Image.Image, box: tuple[int, int, int, int]) -> float:
    """The average brightness (0 to 1) of the part of the picture a logo will cover. A box off the edge counts only what is on the picture."""
    left, top = max(0, box[0]), max(0, box[1])
    right, bottom = min(canvas.width, box[2]), min(canvas.height, box[3])
    if right <= left or bottom <= top:
        return 0.0
    patch = canvas.convert("RGB").crop((left, top, right, bottom)).convert("L").resize((1, 1), Image.BOX)
    return patch.getpixel((0, 0)) / 255.0


def plate_colour(backing: str, brightness: float) -> Optional[tuple[int, int, int, int]]:
    """The plate's colour for a backing choice and the brightness under the logo, or None when there is to be no plate."""
    backing = clean_backing(backing)
    if backing == "none":
        return None
    if backing == "light":
        return _PLATE_LIGHT
    if backing == "dark":
        return _PLATE_DARK
    return _PLATE_DARK if brightness > LIGHT_ABOVE else _PLATE_LIGHT


def draw_plate(canvas: Image.Image, box: tuple[int, int, int, int], backing: str) -> bool:
    """Draws the plate behind a logo that will fill `box`. Returns whether anything was drawn."""
    colour = plate_colour(backing, brightness_under(canvas, box) if clean_backing(backing) == "auto" else 0.0)
    if colour is None:
        return False
    pad = int(min(box[2] - box[0], box[3] - box[1]) * PLATE_PAD)
    plate = Image.new("RGBA", (box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle([(0, 0), (plate.width - 1, plate.height - 1)], radius=max(2, pad), fill=colour)
    x, y = box[0] - pad, box[1] - pad
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(plate.width, canvas.width - x), min(plate.height, canvas.height - y)
    if right <= left or bottom <= top:
        return False
    canvas.alpha_composite(plate.crop((left, top, right, bottom)), (max(0, x), max(0, y)))
    return True
