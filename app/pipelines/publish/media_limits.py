"""Limits a file must meet before it is sent, checked on the server so a post that could never be accepted is stopped with a plain reason
instead of failing at the platform. Only limits the platforms publish are listed; a size, length or shape that is not known for a file is
never held against it, and a limit that is not confirmed is left to the platform to enforce.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.pipelines.publish.spine import platform_key

MB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    image_bytes: Optional[int] = None
    video_bytes: Optional[int] = None
    video_seconds: Optional[float] = None
    #: Narrowest and widest a picture may be, as width divided by height.
    image_ratio: Optional[tuple[float, float]] = None


LIMITS: dict[str, Limits] = {
    "facebook": Limits(image_bytes=10 * MB),
    "threads": Limits(image_bytes=8 * MB, video_bytes=1024 * MB, video_seconds=300),
    "bluesky": Limits(video_bytes=300 * MB),
    # A feed picture is between 4:5 and 1.91:1.
    "instagram": Limits(image_ratio=(0.8, 1.91)),
}

_RATIO_TOLERANCE = 0.01


def _kind(asset) -> str:
    kind = getattr(asset, "kind", None)
    return getattr(kind, "value", kind) or ""


def _size_words(size: float) -> str:
    return f"{size / MB:.0f} MB" if size >= 10 * MB else f"{size / MB:.1f} MB"


def media_problem(platform: str, media: list) -> Optional[str]:
    """Why these files cannot be sent to the platform, in plain words, or None when they can."""
    limits = LIMITS.get(platform_key(platform))
    if not limits:
        return None
    from app.shared.activity.projector import platform_name

    name = platform_name(platform_key(platform)) or platform
    for asset in media:
        kind = _kind(asset)
        size = getattr(asset, "size_bytes", None)
        if kind == "image":
            if limits.image_bytes and size and size > limits.image_bytes:
                return f"A picture is {_size_words(size)} and {name} takes pictures up to {_size_words(limits.image_bytes)}. Use a smaller picture."
            width, height = getattr(asset, "width", None), getattr(asset, "height", None)
            if limits.image_ratio and width and height:
                low, high = limits.image_ratio
                ratio = width / height
                if ratio < low - _RATIO_TOLERANCE or ratio > high + _RATIO_TOLERANCE:
                    return f"{name} takes pictures between 4:5 (tall) and 1.91:1 (wide). Crop the picture to fit."
        elif kind == "video":
            if limits.video_bytes and size and size > limits.video_bytes:
                return f"The video is {_size_words(size)} and {name} takes videos up to {_size_words(limits.video_bytes)}. Use a smaller video."
            seconds = getattr(asset, "duration_s", None)
            if limits.video_seconds and seconds and seconds > limits.video_seconds:
                return f"The video is {int(seconds // 60)} min {int(seconds % 60)} s and {name} takes videos up to {int(limits.video_seconds // 60)} minutes. Use a shorter video."
    return None
