"""Media "quick edit" — trim (video/audio) and crop-to-aspect-ratio
(image/video), using Cloudinary's own URL transformation parameters.
No ffmpeg, no new infra: Cloudinary generates and caches the transformed
bytes on first request to the transformed URL — the same asset host every
upload already goes through, not a separate processing pipeline.

Real, cheap "quick actions" for real usage (crop to fit a platform's
aspect ratio, trim a clip to length) — not a broad video/audio editor.
Manual crop positioning is out of scope here; g_auto (Cloudinary's
content-aware auto-gravity) picks the crop point instead of asking the
user to drag a box, keeping this a one-click action, not a canvas editor.
"""

import logging
import re
from typing import Optional

from app.models.media import MediaKind

logger = logging.getLogger(__name__)

# Real aspect-ratio presets a "quick action" UI offers — not free-form
# pixel dimensions, which would need real dimension math per source asset.
ASPECT_RATIO_PRESETS: dict[str, str] = {
    "original": "",
    "16:9": "16:9",   # landscape / standard YouTube
    "9:16": "9:16",   # vertical / Shorts, Reels, Stories
    "1:1": "1:1",     # square
    "4:5": "4:5",     # portrait feed post
}

# Matches ".../upload/<rest>" so a transformation string can be inserted
# right after "upload/" — the one insertion point every Cloudinary
# delivery URL shares, regardless of resource_type (image/video) or
# whatever folder path comes after it.
_UPLOAD_SEGMENT = re.compile(r"(/upload/)(.*)$")


def build_transformed_url(
    source_url: str,
    *,
    kind: MediaKind,
    aspect_ratio: Optional[str] = None,
    trim_start_s: Optional[float] = None,
    trim_end_s: Optional[float] = None,
) -> str:
    """Inserts Cloudinary transformation parameters into an existing
    delivery URL. Raises ValueError if source_url doesn't look like a real
    Cloudinary URL — never silently returns an untransformed URL as if it
    worked.

    g_auto (content-aware/smart-crop gravity) is used for images only —
    verified live: for video, Cloudinary's tracking-crop runs as an async
    job and the delivery URL 423s ("tracking-crop is pending") until it
    finishes, breaking the "quick action, instant result" premise this
    exists for. Video crop uses plain c_fill (a synchronous center-crop,
    verified live) instead — less smart about the crop point, but
    actually returns something on first request, which matters more here.
    """
    match = _UPLOAD_SEGMENT.search(source_url)
    if not match:
        raise ValueError(f"Not a recognizable Cloudinary delivery URL: {source_url}")

    parts: list[str] = []
    if aspect_ratio and aspect_ratio != "original":
        if aspect_ratio not in ASPECT_RATIO_PRESETS:
            raise ValueError(f"Unsupported aspect ratio preset: {aspect_ratio}")
        gravity = ",g_auto" if kind == MediaKind.IMAGE else ""
        parts.append(f"ar_{aspect_ratio},c_fill{gravity}")
    if trim_start_s is not None:
        parts.append(f"so_{trim_start_s:g}")
    if trim_end_s is not None:
        parts.append(f"eo_{trim_end_s:g}")

    if not parts:
        raise ValueError("No transformation requested — nothing to build.")

    transformation = ",".join(parts)
    prefix = source_url[: match.start(1)]
    rest = match.group(2)
    return f"{prefix}/upload/{transformation}/{rest}"


# png/webp — Stage 2 scope of the ImageAsset export feature
# (pow/audio_image_pipeline/01-image-pipeline-plan.md). mp3/wav — Stage 4
# scope of the AudioAsset export feature (02-audio-pipeline-plan.md).
# svg/pdf/zip (image) and SRT/VTT/JSON transcript + ID3 tags (audio) are
# real, separate Phase-4 work (svg needs a vector source path to exist at
# all, never a rasterize-on-the-fly; pdf/zip/transcript-export need their
# own compilation modules, not a Cloudinary URL param) — not silently
# faked here. Cloudinary's f_<ext> transform param works the same way
# across image/video(audio) resource types, confirmed via its own docs.
_EXPORT_FORMAT_PARAMS: dict[str, str] = {
    "png": "f_png",
    "webp": "f_webp",
    "mp3": "f_mp3",
    "wav": "f_wav",
}


def build_export_url(source_url: str, *, export_format: str) -> str:
    """Same insertion technique as build_transformed_url, for a plain
    format-conversion export (no crop/trim). Raises ValueError for a
    format this function doesn't (yet) support — never silently returns
    the original URL as if the conversion happened."""
    if export_format not in _EXPORT_FORMAT_PARAMS:
        raise ValueError(
            f"build_export_url only supports {sorted(_EXPORT_FORMAT_PARAMS)} today "
            f"— {export_format!r} needs its own real export pipeline, not a "
            "Cloudinary format param."
        )

    match = _UPLOAD_SEGMENT.search(source_url)
    if not match:
        raise ValueError(f"Not a recognizable Cloudinary delivery URL: {source_url}")

    prefix = source_url[: match.start(1)]
    rest = match.group(2)
    return f"{prefix}/upload/{_EXPORT_FORMAT_PARAMS[export_format]}/{rest}"
