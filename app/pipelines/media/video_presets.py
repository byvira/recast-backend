"""Where a finished video is going to be posted, and what usually works there: the best shape, the longest length the
platform accepts and the length that tends to hold attention. These are typical published limits, not promises: platforms
change them, so the advice always says to confirm before publishing. The advice only informs; it never blocks a render."""
from __future__ import annotations

from typing import Optional, TypedDict


class Preset(TypedDict):
    label: str
    sizes: list[str]           # best first
    max_seconds: Optional[int]  # None when the limit is long enough not to matter for a clip
    ideal_seconds: tuple[int, int]
    platform: str              # the content platform name the Drafts flow uses


PRESETS: dict[str, Preset] = {
    "linkedin": {"label": "LinkedIn", "sizes": ["portrait", "square"], "max_seconds": 600, "ideal_seconds": (15, 90), "platform": "LinkedIn"},
    "instagram_reels": {"label": "Instagram Reels", "sizes": ["vertical"], "max_seconds": 180, "ideal_seconds": (15, 60), "platform": "Instagram"},
    "instagram_feed": {"label": "Instagram feed", "sizes": ["portrait", "square"], "max_seconds": 60, "ideal_seconds": (10, 45), "platform": "Instagram"},
    "tiktok": {"label": "TikTok", "sizes": ["vertical"], "max_seconds": 600, "ideal_seconds": (15, 45), "platform": "Instagram"},
    "youtube_shorts": {"label": "YouTube Shorts", "sizes": ["vertical"], "max_seconds": 180, "ideal_seconds": (15, 60), "platform": "YouTube"},
    "youtube": {"label": "YouTube", "sizes": ["landscape"], "max_seconds": None, "ideal_seconds": (60, 600), "platform": "YouTube"},
    "facebook": {"label": "Facebook", "sizes": ["square", "portrait", "vertical"], "max_seconds": 240 * 60, "ideal_seconds": (15, 90), "platform": "Facebook"},
    "x": {"label": "X", "sizes": ["landscape", "square"], "max_seconds": 140, "ideal_seconds": (10, 45), "platform": "Twitter/X"},
}

_SIZE_WORDS = {"vertical": "vertical 9:16", "portrait": "feed 4:5", "square": "square 1:1", "landscape": "landscape 16:9"}
DISCLAIMER = "Platform limits change, so confirm them before you publish."


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds >= 60 else f"{seconds} seconds"


def advice(platform: Optional[str], size: str, duration_s: float) -> list[str]:
    """Plain sentences about this video for that platform. Empty when no platform is chosen or nothing needs saying."""
    preset = PRESETS.get(platform or "")
    if not preset:
        return []
    notes: list[str] = []
    if size not in preset["sizes"]:
        best = " or ".join(_SIZE_WORDS[s] for s in preset["sizes"])
        notes.append(f"{preset['label']} usually looks best as {best}. This video is {_SIZE_WORDS.get(size, size)}.")
    cap = preset["max_seconds"]
    if cap is not None and duration_s > cap:
        notes.append(f"{preset['label']} accepts videos up to {_clock(cap)}. This one is {_clock(duration_s)}, so trim it before posting.")
    low, high = preset["ideal_seconds"]
    if duration_s > high and (cap is None or duration_s <= cap):
        notes.append(f"Videos of {_clock(low)} to {_clock(high)} tend to hold attention best on {preset['label']}.")
    if notes:
        notes.append(DISCLAIMER)
    return notes


def presets_payload() -> list[dict]:
    return [
        {"id": key, "label": p["label"], "sizes": p["sizes"], "max_seconds": p["max_seconds"], "ideal_seconds": list(p["ideal_seconds"]), "platform": p["platform"]}
        for key, p in PRESETS.items()
    ]
