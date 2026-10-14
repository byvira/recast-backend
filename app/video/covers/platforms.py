"""Where a cover goes, as data: the picture size, the file limit and the room kept clear for words.

One row per platform and type. Everything that makes a cover (crop shape, text box, file size) reads this table, so adding a
platform is one more row and no code. Sizes are the platforms' own recommended pixel sizes; `max_bytes` is the platform's file
limit (a cover is saved as a smaller JPEG when the picture would be over it).

`safe` is the share of the picture kept clear on each side for words (left, top, right, bottom): bigger at the bottom of tall
videos, where the platform's own buttons and captions sit over the picture.
"""
from __future__ import annotations

from typing import Any

MB = 1024 * 1024

PLATFORMS: dict[str, dict[str, Any]] = {
    "youtube": {"label": "YouTube thumbnail", "w": 1280, "h": 720, "max_bytes": 2 * MB, "safe": (0.05, 0.06, 0.05, 0.14), "max_chars": 42, "max_lines": 3},
    "youtube_shorts": {"label": "YouTube Shorts cover", "w": 1080, "h": 1920, "max_bytes": 2 * MB, "safe": (0.07, 0.12, 0.07, 0.22), "max_chars": 36, "max_lines": 4},
    "instagram_reels": {"label": "Instagram Reels cover", "w": 1080, "h": 1920, "max_bytes": 8 * MB, "safe": (0.07, 0.14, 0.07, 0.24), "max_chars": 36, "max_lines": 4},
    "tiktok": {"label": "TikTok cover", "w": 1080, "h": 1920, "max_bytes": 8 * MB, "safe": (0.07, 0.12, 0.12, 0.26), "max_chars": 36, "max_lines": 4},
    "instagram_feed": {"label": "Instagram post (4:5)", "w": 1080, "h": 1350, "max_bytes": 8 * MB, "safe": (0.07, 0.07, 0.07, 0.12), "max_chars": 48, "max_lines": 4},
    "instagram_square": {"label": "Instagram square", "w": 1080, "h": 1080, "max_bytes": 8 * MB, "safe": (0.07, 0.07, 0.07, 0.1), "max_chars": 48, "max_lines": 4},
    "facebook": {"label": "Facebook link or video", "w": 1200, "h": 630, "max_bytes": 8 * MB, "safe": (0.06, 0.08, 0.06, 0.12), "max_chars": 48, "max_lines": 3},
    "linkedin": {"label": "LinkedIn post", "w": 1200, "h": 627, "max_bytes": 5 * MB, "safe": (0.06, 0.08, 0.06, 0.12), "max_chars": 52, "max_lines": 3},
    "x": {"label": "X post", "w": 1600, "h": 900, "max_bytes": 5 * MB, "safe": (0.06, 0.08, 0.06, 0.12), "max_chars": 48, "max_lines": 3},
    "threads": {"label": "Threads post", "w": 1080, "h": 1350, "max_bytes": 8 * MB, "safe": (0.07, 0.07, 0.07, 0.12), "max_chars": 48, "max_lines": 4},
    "bluesky": {"label": "Bluesky post", "w": 1200, "h": 630, "max_bytes": 1 * MB, "safe": (0.06, 0.08, 0.06, 0.12), "max_chars": 48, "max_lines": 3},
    "pinterest": {"label": "Pinterest pin", "w": 1000, "h": 1500, "max_bytes": 20 * MB, "safe": (0.07, 0.07, 0.07, 0.12), "max_chars": 48, "max_lines": 4},
    "podcast": {"label": "Podcast cover art", "w": 3000, "h": 3000, "max_bytes": 500 * 1024, "safe": (0.08, 0.08, 0.08, 0.08), "max_chars": 32, "max_lines": 4},
    "blog": {"label": "Blog header", "w": 1200, "h": 630, "max_bytes": 5 * MB, "safe": (0.06, 0.08, 0.06, 0.12), "max_chars": 60, "max_lines": 3},
    "carousel": {"label": "Carousel cover (4:5)", "w": 1080, "h": 1350, "max_bytes": 8 * MB, "safe": (0.07, 0.07, 0.07, 0.12), "max_chars": 44, "max_lines": 4},
}

#: Platforms that take an automatic cover are not claimed here; this table only says what size and limit a cover must have.


def platform(name: str) -> dict[str, Any]:
    if name not in PLATFORMS:
        raise KeyError(name)
    return PLATFORMS[name]


def public_table() -> list[dict[str, Any]]:
    out = []
    for key, row in PLATFORMS.items():
        out.append({"id": key, "label": row["label"], "width": row["w"], "height": row["h"], "max_bytes": row["max_bytes"],
                    "orientation": "tall" if row["h"] > row["w"] * 1.2 else "wide" if row["w"] > row["h"] * 1.2 else "square"})
    return out
