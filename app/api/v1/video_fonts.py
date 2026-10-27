"""A member's own font file for captions and text in a video. The file is checked (a real TrueType or OpenType font, small) and stored
as a library item with its family name read from the file, so a project can use it by id and the render draws it under that name.

The member is responsible for having the right to use the font; nothing here licenses it."""
import io
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.db.mongo import media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.shared.storage import ContentType, upload_file
from app.shared.uploads import read_limited

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_FONT_BYTES = 4 * 1024 * 1024
MAX_FONTS_PER_WORKSPACE = 20
_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true")      # TrueType, OpenType with PostScript outlines, old Apple TrueType


def font_family(data: bytes) -> str:
    """The family name inside a font file. Raises ValueError for a file that is not a usable font."""
    if not data.startswith(_MAGIC):
        raise ValueError("That file is not a TrueType or OpenType font.")
    try:
        from PIL import ImageFont

        family = ImageFont.truetype(io.BytesIO(data), 20).getname()[0]
    except Exception as exc:  # noqa: BLE001
        raise ValueError("That font file could not be read.") from exc
    family = "".join(ch for ch in str(family).strip() if ch.isprintable() and ch not in "{}\\")
    if not family or len(family) > 80:
        raise ValueError("That font has no usable name.")
    return family


@router.post("", status_code=201)
@limiter.limit("10/minute")
async def upload_font(request: Request, file: UploadFile = File(...), ctx: WorkspaceContext = Depends(require("create_content"))) -> dict:
    from app.shared.entitlements import require_storage_room

    data = await read_limited(file, MAX_FONT_BYTES, detail="A font file must be 4 MB or smaller.")
    await require_storage_room(ctx.workspace_id, len(data))
    try:
        family = font_family(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    have = await media_assets.count_documents({"workspace_id": ctx.workspace_id, "font_family": {"$exists": True}})
    if have >= MAX_FONTS_PER_WORKSPACE:
        raise HTTPException(status_code=400, detail=f"A workspace can keep up to {MAX_FONTS_PER_WORKSPACE} fonts. Remove one first.")
    try:
        url = await upload_file(data, ContentType.EXPORT, ctx.user_id, f"font-{uuid4().hex[:12]}.ttf")
    except Exception as exc:  # noqa: BLE001
        logger.error("Font upload failed for workspace %s: %s", ctx.workspace_id, exc)
        raise HTTPException(status_code=502, detail="The font could not be stored. Try again.")
    asset = MediaAsset(
        id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.DOCUMENT, url=url, mime_type="font/ttf", size_bytes=len(data),
        source=MediaSource.UPLOADED, created_by=ctx.user_id, created_at=datetime.now(timezone.utc),
    )
    doc = asset.model_dump()
    doc["font_family"] = family
    await media_assets.insert_one(doc)
    return {"id": asset.id, "family": family, "bytes": len(data)}
