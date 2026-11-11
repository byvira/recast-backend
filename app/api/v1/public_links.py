"""The address behind a one-tap link. Public (no sign-in; the link is the key), so it answers the same for every kind of bad link.

GET only shows what the link would do and changes nothing. POST does it, once."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.core.middleware import limiter
from app.tools import links

router = APIRouter()
_GONE = "This link has already been used, or it has ended."


@router.get("/{token}")
@limiter.limit("60/minute")
async def show_link(request: Request, token: str) -> dict[str, Any]:
    found = await links.show(token)
    if found is None:
        raise HTTPException(status_code=410, detail=_GONE)
    return found


@router.post("/{token}/do")
@limiter.limit("20/minute")
async def do_link(request: Request, token: str) -> dict[str, Any]:
    try:
        return await links.do(token)
    except links.LinkFailed as failed:
        raise HTTPException(status_code=failed.status, detail=failed.message)
