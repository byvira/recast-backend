"""Cloudflare Turnstile check: proves a request came from a real browser session and not a script."""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def turnstile_enabled() -> bool:
    return bool(settings.TURNSTILE_SECRET_KEY)


async def verify_turnstile(token: str, remote_ip: str = "") -> bool:
    """True when Turnstile isn't configured yet (nothing to check against) or the token really passed.

    With a secret set, a missing token fails without calling Cloudflare, and so does any failure to reach Cloudflare."""
    if not turnstile_enabled():
        return True
    if not (token or "").strip():
        return False
    data = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(VERIFY_URL, data=data)
            return bool(res.json().get("success"))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Turnstile verify request failed, treating as unverified: %s", exc)
        return False
