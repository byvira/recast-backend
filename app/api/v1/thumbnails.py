"""Thumbnails for History cards. A recording, a video made from one, or a text run can carry a picture of the member's own
(uploaded) or one made for it (generated from its title and words). The picture is kept on the item itself, so every History
list can show it."""
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.db.mongo import audio_assets, brand_profiles, content_sessions
from app.pipelines.media.image_generation import generate_brand_image
from app.shared.storage import ContentType as UploadContentType, upload_file

logger = logging.getLogger(__name__)
router = APIRouter()

Kind = Literal["audio", "video", "text"]

MAX_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


async def _target(kind: str, item_id: str, clip_id: Optional[str], workspace_id: str) -> tuple[dict, dict, str, str]:
    """The stored document, the filter that finds it, the field that holds the thumbnail, and a topic to draw from."""
    if kind == "text":
        flt = {"session_id": item_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}
        doc = await content_sessions.find_one(flt)
        field = "thumbnail_url"
        topic = doc.get("title") or doc.get("input_text") or "" if doc else ""
    else:
        flt = {"id": item_id, "workspace_id": workspace_id, "deleted": {"$ne": True}}
        doc = await audio_assets.find_one(flt)
        if kind == "video":
            if not clip_id:
                raise HTTPException(status_code=400, detail="Say which video.")
            flt = {**flt, "video_clips.id": clip_id}
            clip = next((c for c in (doc or {}).get("video_clips", []) if c.get("id") == clip_id), None)
            if doc is None or clip is None:
                doc = None
            field = "video_clips.$.thumbnail_url"
            topic = (clip or {}).get("title") or (doc or {}).get("title") or ""
        else:
            field = "thumbnail_url"
            topic = (doc or {}).get("title") or (doc or {}).get("script") or ""
    if not doc:
        raise HTTPException(status_code=404, detail="That item wasn't found.")
    return doc, flt, field, str(topic)[:300]


async def _save(kind: str, flt: dict, field: str, url: Optional[str]) -> None:
    collection = content_sessions if kind == "text" else audio_assets
    now = datetime.now(timezone.utc)
    if url is None:
        await collection.update_one(flt, {"$unset": {field: ""}, "$set": {"updated_at": now}})
    else:
        await collection.update_one(flt, {"$set": {field: url, "updated_at": now}})


@router.post("/upload")
@limiter.limit("20/minute")
async def upload_thumbnail(
    request: Request,
    kind: Kind = Form(...),
    item_id: str = Form(...),
    clip_id: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict[str, Any]:
    """Use a picture of your own as the card's thumbnail."""
    _, flt, field, _topic = await _target(kind, item_id, clip_id, ctx.workspace_id)
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"Use a JPEG, PNG or WebP picture, not '{file.content_type}'.")
    data = await file.read(MAX_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="That picture is empty.")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="The picture must be 5 MB or smaller.")
    try:
        url = await upload_file(data, UploadContentType.THUMBNAIL, ctx.user_id)
    except Exception:  # noqa: BLE001
        logger.exception("Thumbnail upload failed for %s %s", kind, item_id)
        raise HTTPException(status_code=502, detail="The picture couldn't be saved. Try again.")
    await _save(kind, flt, field, url)
    return {"kind": kind, "item_id": item_id, "thumbnail_url": url}


class ItemBody(BaseModel):
    kind: Kind
    item_id: str
    clip_id: Optional[str] = None


@router.post("/generate")
@limiter.limit("6/minute")
async def generate_thumbnail(
    request: Request,
    body: ItemBody,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """Make a thumbnail from the item's title and words. Uses one real picture generation."""
    doc, flt, field, topic = await _target(body.kind, body.item_id, body.clip_id, ctx.workspace_id)
    if not topic.strip():
        raise HTTPException(status_code=400, detail="There is nothing here to draw a picture from yet. Give it a title first.")
    brand = await brand_profiles.find_one({"id": doc.get("brand_id"), "workspace_id": ctx.workspace_id}) if doc.get("brand_id") else None
    if not brand:
        raise HTTPException(status_code=400, detail="This has no brand voice to make a picture from. Upload one instead.")
    asset = await generate_brand_image(topic=topic, brand_profile=brand, workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    if not asset:
        raise HTTPException(status_code=502, detail="The picture couldn't be made right now. Try again in a moment, or upload one.")
    await _save(body.kind, flt, field, asset.url)
    return {"kind": body.kind, "item_id": body.item_id, "thumbnail_url": asset.url, "flagged": bool(asset.qa_flagged)}


@router.post("/clear")
@limiter.limit("30/minute")
async def clear_thumbnail(
    request: Request,
    body: ItemBody,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict[str, Any]:
    """Go back to the card's own default picture."""
    _, flt, field, _topic = await _target(body.kind, body.item_id, body.clip_id, ctx.workspace_id)
    await _save(body.kind, flt, field, None)
    return {"kind": body.kind, "item_id": body.item_id, "thumbnail_url": None}
