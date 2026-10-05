"""Public, unauthenticated read of a shared audio recording or image, plus
what a listener or viewer can do on that page without an account: leave a
timed comment (only when the owner turned that on for this specific link),
and let the owner see anonymous view/play/listen-through counts.

The token is a 24-byte urlsafe secret created by the audio/image
share-link endpoints. An unknown, expired or revoked token all return the
same 404 so nothing can be learned by probing. The response carries only
what a listener or viewer needs: never a workspace id, user id or brand id.
"""

import hashlib
import hmac
import logging
from datetime import date, datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.shared.brand_name import brand_display_name
from app.core.config import settings
from app.core.middleware import limiter
from app.db.mongo import (
    audio_assets,
    audio_comments,
    audio_share_links,
    brand_profiles,
    image_assets,
    image_share_links,
    media_assets,
    share_view_events,
)
from app.models.audio_asset import AudioComment, GuestCommentCreate

logger = logging.getLogger(__name__)
router = APIRouter()

_NOT_AVAILABLE = "This link isn't available. It may have expired or been turned off."
_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_EVENT_TYPES = {"view", "play", "25", "50", "75", "100"}


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


class SharedComment(BaseModel):
    time_s: float
    text: str
    author_name: str
    is_guest: bool
    created_at: datetime


class SharedAssetResponse(BaseModel):
    kind: Literal["audio", "image"]
    title: str
    brand_name: Optional[str] = None
    expires_at: datetime
    media: Optional[SharedMedia] = None          # audio
    transcript: list[SharedWord] = []            # audio
    slides: list[SharedSlide] = []               # image
    allow_comments: bool = False


class ShareEventRequest(BaseModel):
    event_type: str


def _is_live(link: dict) -> bool:
    if link.get("revoked"):
        return False
    expires = link["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


async def _find_link(token: str) -> tuple[Optional[Literal["audio", "image"]], Optional[dict]]:
    audio_link = await audio_share_links.find_one({"token": token})
    if audio_link:
        return "audio", audio_link
    image_link = await image_share_links.find_one({"token": token})
    if image_link:
        return "image", image_link
    return None, None


def _live_link_or_404(link: Optional[dict]) -> dict:
    if not link or not _is_live(link):
        raise HTTPException(status_code=404, detail=_NOT_AVAILABLE)
    return link


async def _brand_name(brand_id: Optional[str], workspace_id: str) -> Optional[str]:
    if not brand_id:
        return None
    doc = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id}, {"identity": 1, "brand_type": 1, "name": 1, "brand_name": 1})
    return brand_display_name(doc) or None


async def _media_by_id(media_ids: list[str]) -> dict[str, SharedMedia]:
    ids = [m for m in media_ids if m]
    if not ids:
        return {}
    found = await media_assets.find({"id": {"$in": ids}}).to_list(length=len(ids))
    return {
        m["id"]: SharedMedia(url=m["url"], mime_type=m.get("mime_type", ""), duration_s=m.get("duration_s"))
        for m in found
    }


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _visitor_hash(request: Request) -> str:
    """A stable, anonymous per-day fingerprint — real enough to dedupe a
    reload without ever storing the raw address it came from. Rotates
    with the calendar day (via settings.SECRET_KEY, not a stored salt), so
    yesterday's fingerprint can't be replayed today or linked across days."""
    raw = f"{date.today().isoformat()}:{_client_ip(request)}:{request.headers.get('user-agent', '')}"
    return hmac.new(settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]


async def _verify_turnstile(token: str, request: Request) -> bool:
    """True when Turnstile isn't configured yet (nothing to check against)
    or the token really passed; False only on a real, confirmed failure."""
    if not settings.TURNSTILE_SECRET_KEY:
        return True
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(_TURNSTILE_VERIFY_URL, data={
                "secret": settings.TURNSTILE_SECRET_KEY, "response": token, "remoteip": _client_ip(request),
            })
            return bool(res.json().get("success"))
    except httpx.HTTPError as exc:
        logger.warning("Turnstile verify request failed, treating as unverified: %s", exc)
        return False


@router.get("/{token}", response_model=SharedAssetResponse)
@limiter.limit("60/minute")
async def get_shared_asset(request: Request, token: str) -> SharedAssetResponse:
    kind, link = await _find_link(token)
    link = _live_link_or_404(link)

    if kind == "audio":
        asset = await audio_assets.find_one(
            {"id": link["audio_asset_id"], "workspace_id": link["workspace_id"]},
        )
        if not asset:
            raise HTTPException(status_code=404, detail=_NOT_AVAILABLE)
        media_id = asset.get("approved_master_media_id") or asset.get("media_id")
        media = (await _media_by_id([media_id])).get(media_id) if media_id else None
        return SharedAssetResponse(
            kind="audio",
            title=asset.get("title", "Untitled recording"),
            brand_name=await _brand_name(asset.get("brand_id"), asset["workspace_id"]),
            expires_at=link["expires_at"],
            media=media,
            transcript=[
                SharedWord(word=w["word"], start_s=w["start_s"], end_s=w["end_s"])
                for w in asset.get("transcript", [])
            ],
            allow_comments=bool(link.get("allow_comments")),
        )

    asset = await image_assets.find_one(
        {"id": link["image_asset_id"], "workspace_id": link["workspace_id"]},
    )
    if not asset:
        raise HTTPException(status_code=404, detail=_NOT_AVAILABLE)
    slides = sorted(asset.get("slides", []), key=lambda s: s.get("slide_number", 0))
    media = await _media_by_id([s.get("media_id") for s in slides])
    return SharedAssetResponse(
        kind="image",
        title=asset.get("title", "Untitled image"),
        brand_name=await _brand_name(asset.get("brand_id"), asset["workspace_id"]),
        expires_at=link["expires_at"],
        slides=[
            SharedSlide(
                slide_number=s.get("slide_number", i + 1),
                title=s.get("title", ""),
                media=media.get(s.get("media_id")),
            )
            for i, s in enumerate(slides)
        ],
    )


@router.get("/{token}/comments", response_model=list[SharedComment])
@limiter.limit("60/minute")
async def list_shared_comments(request: Request, token: str) -> list[SharedComment]:
    """Only ever the approved ones — an unapproved guest note is invisible
    to every other viewer until the owner approves it."""
    kind, link = await _find_link(token)
    link = _live_link_or_404(link)
    if kind != "audio":
        return []
    docs = await audio_comments.find(
        {"audio_asset_id": link["audio_asset_id"], "workspace_id": link["workspace_id"], "approved": True},
    ).sort("time_s", 1).to_list(length=500)
    return [
        SharedComment(time_s=d["time_s"], text=d["text"], author_name=d["user_name"], is_guest=d.get("is_guest", False), created_at=d["created_at"])
        for d in docs
    ]


@router.post("/{token}/comments", response_model=SharedComment, status_code=201)
@limiter.limit("5/minute")
async def create_shared_comment(request: Request, token: str, body: GuestCommentCreate) -> SharedComment:
    kind, link = await _find_link(token)
    link = _live_link_or_404(link)
    if kind != "audio" or not link.get("allow_comments"):
        raise HTTPException(status_code=403, detail="Comments aren't turned on for this link.")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write something first.")
    if len(text) > 1000:
        raise HTTPException(status_code=400, detail="Keep it under 1000 characters.")
    if body.time_s < 0:
        raise HTTPException(status_code=400, detail="The time can't be before the start.")

    guest_name = (body.guest_name or "A listener").strip()[:80] or "A listener"
    now = datetime.now(timezone.utc)
    approved = not link.get("hold_for_approval", True)

    if body.website.strip():
        # A real bot filled in the field a real person never sees. Answer
        # exactly as if it worked, so it never learns which check caught it.
        return SharedComment(time_s=body.time_s, text=text, author_name=guest_name, is_guest=True, created_at=now)

    if not await _verify_turnstile(body.turnstile_token, request):
        raise HTTPException(status_code=400, detail="That didn't verify. Reload the page and try again.")

    comment = AudioComment(
        id=uuid4().hex, audio_asset_id=link["audio_asset_id"], workspace_id=link["workspace_id"],
        user_id="", user_name=guest_name, time_s=body.time_s, text=text, created_at=now,
        is_guest=True, approved=approved,
    )
    await audio_comments.insert_one(comment.model_dump())
    return SharedComment(time_s=comment.time_s, text=comment.text, author_name=comment.user_name, is_guest=True, created_at=comment.created_at)


@router.post("/{token}/event", status_code=204)
@limiter.limit("30/minute")
async def record_share_event(request: Request, token: str, body: ShareEventRequest) -> None:
    """Anonymous, real, deduped per day per visitor — never a raw address
    or user agent stored, never a running count a viewer's own reload
    could inflate."""
    if body.event_type not in _EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"event_type must be one of: {', '.join(sorted(_EVENT_TYPES))}.")
    _, link = await _find_link(token)
    _live_link_or_404(link)
    try:
        await share_view_events.insert_one({
            "token": token, "event_type": body.event_type, "day": date.today().isoformat(),
            "visitor_hash": _visitor_hash(request), "created_at": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        pass  # a real repeat view/play today from the same visitor, not a new one
    return None
