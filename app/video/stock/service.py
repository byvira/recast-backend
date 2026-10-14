"""Using a stock clip: fetch it from its source by id, keep it in the workspace's own storage with its licence and author recorded on
it, and place it in a project as a cutaway. A clip is never hot-linked, so a video keeps working if the source changes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from app.core.config import settings
from app.db.mongo import media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.shared.storage import ContentType as UploadContentType, upload_file_detailed
from app.video import edl
from app.video.stock import providers

logger = logging.getLogger(__name__)

MAX_BYTES = 80 * 1024 * 1024
FADE_F = 6


async def download(url: str, *, limit: int = MAX_BYTES) -> bytes:
    """The clip's bytes, refused if the source says or turns out to be bigger than the limit."""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", url) as res:
            if res.status_code != 200:
                raise HTTPException(status_code=502, detail="The stock clip could not be downloaded right now. Please try again.")
            declared = int(res.headers.get("content-length") or 0)
            if declared > limit:
                raise HTTPException(status_code=413, detail="That stock clip is too large to use.")
            chunks: list[bytes] = []
            total = 0
            async for part in res.aiter_bytes():
                total += len(part)
                if total > limit:
                    raise HTTPException(status_code=413, detail="That stock clip is too large to use.")
                chunks.append(part)
    return b"".join(chunks)


async def existing(workspace_id: str, provider: str, clip_id: str) -> Optional[dict]:
    return await media_assets.find_one({"workspace_id": workspace_id, "source": MediaSource.STOCK.value, "stock.provider": provider, "stock.id": clip_id})


async def use_clip(
    *, workspace_id: str, user_id: str, provider: str, clip_id: str, query: str = "", fetch=None, get_one=None,
) -> MediaAsset:
    """The stock clip as a library item with its licence, author and source page recorded. Using the same clip twice returns the
    item that already exists."""
    have = await existing(workspace_id, provider, clip_id)
    if have:
        return MediaAsset(**{k: v for k, v in have.items() if k != "_id"})
    clip = await (get_one or providers.get_one)(provider, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="That stock clip is no longer available from its source.")
    data = await (fetch or download)(clip["download_url"])
    try:
        info = await upload_file_detailed(data, UploadContentType.VIDEO, user_id, filename=f"{provider}-{clip_id}.mp4")
    except Exception as exc:  # noqa: BLE001
        logger.error("Stock clip upload failed for workspace %s: %s", workspace_id, exc)
        raise HTTPException(status_code=502, detail="The stock clip could not be saved. Please try again.") from exc
    media = MediaAsset(
        id=str(uuid.uuid4()), workspace_id=workspace_id, kind=MediaKind.VIDEO, url=info["url"], mime_type="video/mp4",
        width=info.get("width") or clip.get("width") or None, height=info.get("height") or clip.get("height") or None,
        duration_s=info.get("duration_s") or clip.get("duration_s") or None, source=MediaSource.STOCK, created_by=user_id,
        created_at=datetime.now(timezone.utc), size_bytes=info.get("bytes") or len(data), poster_url=clip.get("poster_url"),
        stock={
            "provider": provider, "id": clip_id, "page_url": clip.get("page_url"), "author": clip.get("author"), "author_url": clip.get("author_url"),
            "license": clip.get("license"), "license_url": clip.get("license_url"), "query": query[:100], "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await media_assets.insert_one(media.model_dump())
    return media


def credit_line(media: dict) -> Optional[str]:
    """The credit to show for a stock clip ("Footage: Name via Pixabay"), or None when the item is not stock."""
    s = media.get("stock") or {}
    if not s.get("provider"):
        return None
    who = s.get("author") or "the creator"
    return f"Footage: {who} via {str(s['provider']).title()}"


def insert_clip(edit: dict[str, Any], media: dict[str, Any], *, at_ms: int, duration_ms: Optional[int] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The project's edit with the clip added as a cutaway over the main picture (its own sound off), and the clip as placed. The
    clip never runs past its own length, and never past the end of the project when it starts inside it."""
    fps = edit["fps"]
    at_f = max(0, edl.seconds_to_frames(max(0, at_ms) / 1000, fps))
    own_f = max(1, edl.seconds_to_frames(float(media.get("duration_s") or 4.0), fps))
    want_f = edl.seconds_to_frames(duration_ms / 1000, fps) if duration_ms else own_f
    dur_f = max(1, min(own_f, want_f))
    end = edl.end_frame(edit)
    if end and at_f < end:
        dur_f = max(1, min(dur_f, end - at_f))
    fade = min(FADE_F, dur_f // 4)
    clip = {
        "id": f"b{uuid.uuid4().hex[:8]}", "media_id": media["id"], "at_f": at_f, "dur_f": dur_f, "src_in_f": 0, "speed": 1.0, "fit": "cover",
        "fade_in_f": fade, "fade_out_f": fade, "audio": False, "gain_db": 0.0, "motion": media.get("kind") in ("image", MediaKind.IMAGE),
    }
    out = {**edit, "video": [*edit.get("video", []), clip]}
    return edl.validate(out), clip
