"""File names and archive layout for exports. Pure, no network or database.

Every exported file is named  post-title_platform_pipeline_type.ext
  - post-title: the first line of the post (or the asset's title) as a safe slug
  - platform:   where it is for ("linkedin", "instagram"), or "library" when it has none
  - pipeline:   text, audio or image
  - type:       what the file is ("post", "narration", "card", "voiceover", "image", "video", ...)
Text is written as individual .txt files; media keeps its real format (.mp3, .wav, .png, .mp4 ...).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

MAX_TITLE_CHARS = 60

# mime type -> extension. Only real, native formats; unknown types fall back to the subtype.
MIME_EXTENSIONS = {
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "image/svg+xml": "svg",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/webm": "webm", "audio/ogg": "ogg", "audio/flac": "flac",
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm", "video/x-msvideo": "avi",
}

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def slugify(text: Optional[str], *, fallback: str = "untitled", max_chars: int = MAX_TITLE_CHARS) -> str:
    """Lowercase letters, digits and single hyphens. Accents are folded; other scripts are kept
    (a Tamil or Hindi title should not become empty), anything unsafe in a file name is dropped."""
    value = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    value = re.sub(r"[\/:*?\"<>|\x00-\x1f]", " ", value)
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_-]+", "-", value).strip("-")
    value = value[:max_chars].strip("-")
    if not value or value in _WINDOWS_RESERVED:
        return fallback
    return value


def extension_for(mime_type: Optional[str], default: str = "bin") -> str:
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    if "/" in mime:
        sub = re.sub(r"[^a-z0-9]", "", mime.split("/", 1)[1])
        if 0 < len(sub) <= 5:
            return sub
    return default


def export_filename(title: Optional[str], platform: Optional[str], pipeline: str, kind: str, ext: str) -> str:
    """post-title_platform_pipeline_type.ext"""
    parts = [
        slugify(title),
        slugify(platform, fallback="library", max_chars=24),
        slugify(pipeline, fallback="text", max_chars=12),
        slugify(kind, fallback="file", max_chars=16),
    ]
    return "_".join(parts) + "." + ext.lstrip(".").lower()


def unique_names(names: Iterable[str]) -> list[str]:
    """The same names, with -2, -3 ... added before the extension to any that repeat."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        key = name.lower()
        if key not in seen:
            seen[key] = 1
            out.append(name)
            continue
        seen[key] += 1
        stem, dot, ext = name.rpartition(".")
        out.append(f"{stem}-{seen[key]}.{ext}" if dot else f"{name}-{seen[key]}")
    return out


def first_line_title(content: Optional[str], fallback: str = "untitled") -> str:
    for line in (content or "").splitlines():
        if line.strip():
            return line.strip()
    return fallback
