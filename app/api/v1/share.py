"""Public, unauthenticated read of a shared audio recording or image.

One route, `GET /api/v1/share/{token}`, behind the `/share/[token]` page.
The token is a 24-byte urlsafe secret created by the audio/image
share-link endpoints. An unknown, expired or revoked token all return the
same 404 so nothing can be learned by probing. The response carries only
what a listener or viewer needs: never a workspace id, user id or brand id.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.middleware import limiter
from app.db.mongo import (
    audio_assets,
    audio_share_links,
    brand_profiles,
    image_assets,
    image_share_links,
    media_assets,
)

router = APIRouter()

_NOT_AVAILABLE = "This link isn't available. It may have expired or been turned off."


class SharedMedia(BaseModel):
    url: str
    mime_type: str
    duration_s: Optional[float] = None


class SharedWord(BaseModel):
    word: str
    start_s: float
    end_s: float


class SharedSlide(BaseModel):
    slide_number: int
    title: str
    media: Optional[SharedMedia] = None


class SharedAssetResponse(BaseModel):
    kind: Literal["audio", "image"]
    title: str
    brand_name: Optional[str] = None
    expires_at: datetime
    media: Optional[SharedMedia] = None          # audio
    transcript: list[SharedWord] = []            # audio
    slides: list[SharedSlide] = []               # image


def _is_live(link: dict) -> bool:
    if link.get("revoked"):
        return False
    expires = link["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


async def _brand_name(brand_id: Optional[str], workspace_id: str) -> Optional[str]:
    if not brand_id:
        return None
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id}, {"identity": 1})
    identity = (doc or {}).get("identity") or {}
    name = identity.get("productName") or identity.get("name")
    return name if isinstance(name, str) and name.strip() else None


async def _media_by_id(media_ids: list[str]) -> dict[str, SharedMedia]:
    ids = [m for m in media_ids if m]
    if not ids:
        return {}
    found = await media_assets.find({"id": {"$in": ids}}).to_list(length=len(ids))
    return {
        m["id"]: SharedMedia(url=m["url"], mime_type=m.get("mime_type", ""), duration_s=m.get("duration_s"))
        for m in found
    }


@router.get("/{token}", response_model=SharedAssetResponse)
@limiter.limit("60/minute")
async def get_shared_asset(request: Request, token: str) -> SharedAssetResponse:
    audio_link = await audio_share_links.find_one({"token": token})
    if audio_link:
        if not _is_live(audio_link):
            raise HTTPException(status_code=404, detail=_NOT_AVAILABLE)
        asset = await audio_assets.find_one(
            {"id": audio_link["audio_asset_id"], "workspace_id": audio_link["workspace_id"]},
        )
        if not asset:
            raise HTTPException(status_code=404, detail=_NOT_AVAILABLE)
        media_id = asset.get("approved_master_media_id") or asset.get("media_id")
        media = (await _media_by_id([media_id])).get(media_id) if media_id else None
        return SharedAssetResponse(
            kind="audio",
            title=asset.get("title", "Untitled recording"),
            brand_name=await _brand_name(asset.get("brand_id"), asset["workspace_id"]),
            expires_at=audio_link["expires_at"],
            media=media,
            transcript=[
                SharedWord(word=w["word"], start_s=w["start_s"], end_s=w["end_s"])
                for w in asset.get("transcript", [])
            ],
        )

    image_link = await image_share_links.find_one({"token": token})
    if image_link:
        if not _is_live(image_link):
            raise HTTPException(status_code=404, detail=_NOT_AVAILABLE)
        asset = await image_assets.find_one(
            {"id": image_link["image_asset_id"], "workspace_id": image_link["workspace_id"]},
        )
        if not asset:
            raise HTTPException(status_code=404, detail=_NOT_AVAILABLE)
        slides = sorted(asset.get("slides", []), key=lambda s: s.get("slide_number", 0))
        media = await _media_by_id([s.get("media_id") for s in slides])
        return SharedAssetResponse(
            kind="image",
            title=asset.get("title", "Untitled image"),
            brand_name=await _brand_name(asset.get("brand_id"), asset["workspace_id"]),
            expires_at=image_link["expires_at"],
            slides=[
                SharedSlide(
                    slide_number=s.get("slide_number", i + 1),
                    title=s.get("title", ""),
                    media=media.get(s.get("media_id")),
                )
                for i, s in enumerate(slides)
            ],
        )

    raise HTTPException(status_code=404, detail=_NOT_AVAILABLE)
