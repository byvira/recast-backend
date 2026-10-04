"""Ops Dashboard banners a staff member has closed.

Each person's choices are stored on their own user record (`ops_banners`), so a closed banner stays closed on every browser
and device. A banner is identified by a short id (for example "catalog-attention"). Besides the yes or no, the banner's
`items` (the platforms it was about) are kept, so it can come back when something new shows up instead of staying hidden
forever.
"""

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import users

router = APIRouter()

_BANNER_ID = re.compile(r"^[a-z0-9_:-]{1,80}$")
MAX_ITEMS = 200
MAX_BANNERS = 100


class BannerChoice(BaseModel):
    dismissed: bool
    items: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)


def _check_id(banner_id: str) -> None:
    if not _BANNER_ID.match(banner_id):
        raise HTTPException(status_code=422, detail="That banner name is not valid.")


def _tidy(items: list[str]) -> list[str]:
    seen: list[str] = []
    for raw in items:
        value = str(raw).strip()[:80]
        if value and value not in seen:
            seen.append(value)
    return seen


@router.get("")
@limiter.limit("60/minute")
async def list_banners(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Every banner this person has closed, with what it was about when they closed it."""
    doc = await users.find_one({"id": user["id"]}, {"ops_banners": 1}) or {}
    stored = doc.get("ops_banners") or {}
    return {
        "banners": {
            banner_id: {"dismissed": bool(choice.get("dismissed")), "items": list(choice.get("items") or [])}
            for banner_id, choice in stored.items()
            if isinstance(choice, dict)
        }
    }


@router.put("/{banner_id}")
@limiter.limit("60/minute")
async def save_banner(
    request: Request, banner_id: str, body: BannerChoice, user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """Close a banner (`dismissed: true` with the items it was about) or bring it back (`dismissed: false`)."""
    _check_id(banner_id)
    doc = await users.find_one({"id": user["id"]}, {"ops_banners": 1}) or {}
    stored = doc.get("ops_banners") or {}
    if banner_id not in stored and len(stored) >= MAX_BANNERS:
        raise HTTPException(status_code=422, detail="Too many closed banners. Bring some back first.")
    choice = {"dismissed": body.dismissed, "items": _tidy(body.items) if body.dismissed else []}
    await users.update_one({"id": user["id"]}, {"$set": {f"ops_banners.{banner_id}": choice}})
    return {"banner_id": banner_id, **choice}
