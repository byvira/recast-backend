"""What goes in a Library export, and how the ZIP is put together. Pure: it plans the files from
documents already read from the database and assembles bytes the caller already downloaded, so
the rules can be tested without a network or a database.

Layout:  text/  one .txt per post   |   media/  audio, images and video in their native format
Every file is named post-title_platform_pipeline_type.ext (see naming.py).
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Iterable, Optional

from app.pipelines.export import naming

MAX_MEDIA_FILES = 200


@dataclass
class PlannedFile:
    folder: str                 # "text" or "media"
    name: str                   # final, unique within its folder
    text: Optional[str] = None  # set for text files
    url: Optional[str] = None   # set for media to download
    label: str = ""             # shown in the README when a file could not be included


def _piece_pipeline(piece: dict) -> str:
    value = str(piece.get("pipeline_type") or "text").lower()
    return value if value in ("text", "audio", "image", "video") else "text"


def _media_kind(media: dict) -> str:
    kind = str(media.get("kind") or "").lower()
    if kind in ("image", "audio", "video"):
        return kind
    major = str(media.get("mime_type") or "").split("/")[0]
    return major if major in ("image", "audio", "video") else "file"


def plan_library_export(
    pieces: Iterable[dict],
    audio_assets: Iterable[dict] = (),
    image_assets: Iterable[dict] = (),
    media_by_id: Optional[dict[str, dict]] = None,
) -> list[PlannedFile]:
    media_by_id = media_by_id or {}
    planned: list[PlannedFile] = []

    for p in pieces:
        title = naming.first_line_title(p.get("content"))
        platform = p.get("platform")
        pipeline = _piece_pipeline(p)
        planned.append(PlannedFile(
            folder="text",
            name=naming.export_filename(title, platform, pipeline, "post", "txt"),
            text=str(p.get("content") or ""),
            label=title,
        ))
        for media in (p.get("media") or [])[:1]:
            if not media.get("url"):
                continue
            planned.append(PlannedFile(
                folder="media",
                name=naming.export_filename(
                    title, platform, pipeline, _media_kind(media), naming.extension_for(media.get("mime_type"))
                ),
                url=media["url"],
                label=title,
            ))

    for a in audio_assets:
        media = media_by_id.get(a.get("media_id") or "")
        if not media or not media.get("url"):
            continue
        kind = {
            "script_tts": "narration", "dialogue": "dialogue", "uploaded": "recording", "rss_import": "episode",
        }.get(str(a.get("source_type") or ""), "audio")
        title = a.get("title") or "audio"
        planned.append(PlannedFile(
            folder="media",
            name=naming.export_filename(title, None, "audio", kind, naming.extension_for(media.get("mime_type"), "mp3")),
            url=media["url"],
            label=title,
        ))

    for a in image_assets:
        slides = a.get("slides") or []
        for slide in slides:
            media = media_by_id.get(slide.get("media_id") or "")
            if not media or not media.get("url"):
                continue
            title = a.get("title") or "image"
            if len(slides) > 1:
                title = f"{title} {slide.get('slide_number', '')}".strip()
            kind = "card" if (slide.get("text_content") or {}).get("headline") else "image"
            planned.append(PlannedFile(
                folder="media",
                name=naming.export_filename(title, None, "image", kind, naming.extension_for(media.get("mime_type"), "png")),
                url=media["url"],
                label=title,
            ))

    return _with_unique_names(planned)


def _with_unique_names(files: list[PlannedFile]) -> list[PlannedFile]:
    for folder in ("text", "media"):
        group = [f for f in files if f.folder == folder]
        for f, name in zip(group, naming.unique_names(f.name for f in group)):
            f.name = name
    return files


def within_limits(planned: list[PlannedFile]) -> tuple[list[PlannedFile], int]:
    """All text, and the first MAX_MEDIA_FILES media files. Also returns how many media were left out."""
    text = [f for f in planned if f.folder == "text"]
    media = [f for f in planned if f.folder == "media"]
    return text + media[:MAX_MEDIA_FILES], max(0, len(media) - MAX_MEDIA_FILES)


def readme(included_text: int, included_media: int, failed: list[str], skipped_for_limit: int) -> str:
    lines = [
        "Recast library export",
        "",
        f"text/   {included_text} post{'s' if included_text != 1 else ''} as .txt files",
        f"media/  {included_media} file{'s' if included_media != 1 else ''} in their original format",
        "",
        "File names follow: post-title_platform_pipeline_type",
    ]
    if failed:
        lines += ["", "These files could not be included (they could not be downloaded):", *[f"  - {f}" for f in failed]]
    if skipped_for_limit:
        lines += ["", f"{skipped_for_limit} more media files were left out to keep the download a sensible size."]
    return "\n".join(lines) + "\n"


def build_zip(files: list[PlannedFile], media_bytes: dict[str, bytes], failed: list[str], skipped_for_limit: int = 0) -> bytes:
    """`media_bytes` is keyed by the media file's final name. Media with no bytes is skipped."""
    buf = io.BytesIO()
    text_count = media_count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if f.folder == "text":
                zf.writestr(f"text/{f.name}", f.text or "")
                text_count += 1
            elif f.name in media_bytes:
                zf.writestr(f"media/{f.name}", media_bytes[f.name])
                media_count += 1
        zf.writestr("README.txt", readme(text_count, media_count, failed, skipped_for_limit))
    return buf.getvalue()
