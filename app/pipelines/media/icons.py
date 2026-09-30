"""Icons for image cards, drawn from the bundled Lucide icon font.

The font (fonts/lucide.ttf, ISC licence, see fonts/LUCIDE-LICENSE.txt) maps
each icon name to one private-use character, so an icon is drawn like a letter
with Pillow: no SVG library is needed, and the browser draws the very same
glyphs from the same font, so the picker preview matches the finished image.

lucide_icons.json lists every icon as {name, cp (character code), tags}. It is
generated from the font package's own codepoints and tags files.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

from PIL import ImageFont

_DIR = os.path.dirname(__file__)
_ICON_FONT_PATH = os.path.join(_DIR, "fonts", "lucide.ttf")
_ICON_INDEX_PATH = os.path.join(_DIR, "lucide_icons.json")

# Used for the illustration accent when the member has not picked an icon.
DEFAULT_ACCENT_ICON = "sparkles"


@lru_cache(maxsize=1)
def _index() -> list[dict]:
    with open(_ICON_INDEX_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _by_name() -> dict[str, int]:
    return {item["name"]: item["cp"] for item in _index()}


def list_icons() -> list[dict]:
    """Every available icon: {name, cp, tags}."""
    return _index()


def is_known_icon(name: Optional[str]) -> bool:
    return bool(name) and name in _by_name()


def icon_char(name: str) -> str:
    """The single character that draws this icon in the icon font."""
    return chr(_by_name()[name])


def load_icon_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_ICON_FONT_PATH, size=size)
