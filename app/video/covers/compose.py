"""Making a cover: the platform-shaped picture, the words and the brand design as editable layers, saved as a picture the
Image editor opens.

A cover is an ordinary picture project (one slide whose clean backdrop is the cropped frame and whose layers are the brand band,
the words and the logo), so everything the member can do to a picture can be done to it. The finished file is drawn from those
layers and, when it would be over the platform's size limit, saved as a smaller JPEG.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image

from app.core.workspace import WorkspaceContext
from app.db.mongo import image_assets, media_assets
from app.models.image_asset import ImageApprovalStatus, ImageAsset, ImageSourceType, Slide
from app.models.media import ORIGIN_OWN, ORIGIN_TEMPLATE, MediaSource
from app.pipelines.media.image_layers import default_layers, render_layers
from app.shared.heavy_work import run_heavy
from app.video.covers import crop as C
from app.video.covers.platforms import platform as platform_row


def _flat_background(size: tuple[int, int], rgb: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _rgb(hex_value: str) -> tuple[int, int, int]:
    try:
        v = hex_value.lstrip("#")
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except (ValueError, IndexError):
        return 15, 23, 42


async def make_cover(
    ctx: WorkspaceContext, *, platform_id: str, brand_id: str, text: str, accent: str = "", title: str = "",
    frame_bytes: Optional[bytes] = None, faces: Optional[list[dict[str, float]]] = None, backdrop: str = "primary",
    origin: Optional[dict[str, Any]] = None, link: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """One cover. With `frame_bytes` it is cut from that picture (around the faces when they are known); without, it is the
    brand colour (`backdrop` is "primary", "secondary" or "dark"). Returns the picture project and its finished file."""
    from app.api.v1.image_assets import (
        _brand_tokens_from, _fetch_layer_assets, _get_brand_profile, _record_first_version, _store_image,
    )

    row = platform_row(platform_id)
    size = (row["w"], row["h"])
    brand = await _get_brand_profile(brand_id, ctx.workspace_id)
    tokens = _brand_tokens_from(brand)
    notes: list[str] = []

    if frame_bytes:
        picture, mode, _ = await run_heavy(C.fit_frame, frame_bytes, size[0], size[1], faces)
        if mode == "blur":
            notes.append("The faces would not fit this shape, so the whole picture is shown on a soft backdrop.")
        if faces is None:
            notes.append("Faces were not checked, so the picture is cut around the middle.")
        default_origin = origin or {"origin": ORIGIN_OWN, "contains_ai": False, "contains_stock": False}
    else:
        pick = {"primary": tokens.primary_hex, "secondary": tokens.secondary_hex, "dark": "#0f172a"}.get(backdrop, tokens.primary_hex)
        picture = await run_heavy(_flat_background, size, _rgb(pick))
        default_origin = {"origin": ORIGIN_TEMPLATE, "contains_ai": False, "contains_stock": False}

    backdrop_media = await _store_image(picture, workspace_id=ctx.workspace_id, user_id=ctx.user_id, size=size, mime="image/jpeg",
                                        source=MediaSource.RENDERED)
    await media_assets.update_one({"id": backdrop_media.id}, {"$set": default_origin})

    visual = brand.get("visual_identity") or {}
    layers = default_layers(size=size, brand=tokens, headline=text, show_text=bool(text.strip()), accent_keyword=accent,
                            has_logo=bool(visual.get("logo_url")))
    assets = await _fetch_layer_assets(layers, brand, ctx.workspace_id)
    rendered = await run_heavy(render_layers, size=size, background_bytes=picture, layers=layers, brand=tokens, assets=assets, effects=None)
    try:
        final_bytes, mime = await run_heavy(C.under_limit, rendered, row["max_bytes"])
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"This cover can not be made small enough for {row['label']}. Try a plainer picture.",
        ) from exc
    if mime != "image/png":
        notes.append(f"Saved as a JPEG to stay under the file size limit for {row['label']}.")
    finished = await _store_image(final_bytes, workspace_id=ctx.workspace_id, user_id=ctx.user_id, size=size, mime=mime,
                                  source=MediaSource.RENDERED)
    await media_assets.update_one({"id": finished.id}, {"$set": default_origin})

    now = datetime.now(timezone.utc)
    asset_id = str(uuid4())
    label = title.strip() or text.strip() or "Cover"
    asset = ImageAsset(
        id=asset_id, workspace_id=ctx.workspace_id, brand_id=brand_id, created_by=ctx.user_id, created_at=now, updated_at=now,
        title=f"{label[:60]} · {row['label']}", source_type=ImageSourceType.UPLOADED, brand_tokens=tokens.model_dump(),
        slides=[Slide(slide_number=1, title=label[:80], slide_type="cover", media_id=finished.id, background_media_id=backdrop_media.id,
                      layers=layers, width=size[0], height=size[1])],
        approval_status=ImageApprovalStatus.PENDING,
    )
    doc = asset.model_dump()
    doc["cover_for"] = {"platform": platform_id, **(link or {})}
    await image_assets.insert_one(doc)
    await _record_first_version(asset)
    return {
        "image_asset_id": asset_id, "media_id": finished.id, "url": finished.url, "width": size[0], "height": size[1],
        "platform": platform_id, "bytes": len(final_bytes), "mime": mime, "text": text, "notes": notes, **default_origin,
    }
