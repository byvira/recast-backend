"""The public face of a connected account: its display name, picture, handle and follower count, read from the platform itself.

Used by the Publish Workspace so a preview shows the real account, not a made-up one. Each platform is asked once after it connects,
and again at most once a day for accounts that have none saved. Everything is best effort: a platform that does not answer leaves the
account as it was and the screen falls back to initials. Pictures are only ever kept as https addresses.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.core.config import settings
from app.db.mongo import workspace_connections
from app.pipelines.publish.token_store import get_token

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v25.0"
THREADS = "https://graph.threads.net/v1.0"
BSKY_PUBLIC = "https://public.api.bsky.app/xrpc"
LINKEDIN_USERINFO = "https://api.linkedin.com/v2/userinfo"
YOUTUBE_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"

TIMEOUT = 8.0
RECHECK_AFTER = timedelta(hours=24)
#: Platforms whose profile can be read.
SUPPORTED = ("instagram", "facebook", "threads", "bluesky", "linkedin", "youtube")


def _https(value: object) -> Optional[str]:
    """A picture or page address worth keeping: https only, a sensible length."""
    return value if isinstance(value, str) and value.startswith("https://") and len(value) <= 2000 else None


def _count(value: object) -> Optional[int]:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _clean(name: object) -> Optional[str]:
    text = " ".join(str(name or "").split())
    return text[:120] or None


async def fetch_profile(platform: str, token: dict, client: httpx.AsyncClient) -> dict:
    """What the platform says about the connected account. Keys left out are unknown. Never raises for a platform that is unreachable."""
    access = token.get("access_token") or ""
    identity = token.get("platform_user_id") or ""
    result: dict = {}

    if platform == "instagram":
        r = await client.get(f"{GRAPH}/{identity}", params={"fields": "username,name,profile_picture_url,followers_count", "access_token": access})
        if r.status_code == 200:
            d = r.json()
            result = {"username": _clean(d.get("username")), "display_name": _clean(d.get("name")), "avatar_url": _https(d.get("profile_picture_url")), "followers": _count(d.get("followers_count"))}
    elif platform == "facebook":
        r = await client.get(f"{GRAPH}/{identity}", params={"fields": "name,fan_count,picture.type(large){url}", "access_token": access})
        if r.status_code == 200:
            d = r.json()
            result = {"display_name": _clean(d.get("name")), "avatar_url": _https(((d.get("picture") or {}).get("data") or {}).get("url")), "followers": _count(d.get("fan_count"))}
    elif platform == "threads":
        r = await client.get(f"{THREADS}/{identity}", params={"fields": "username,name,threads_profile_picture_url", "access_token": access})
        if r.status_code == 200:
            d = r.json()
            result = {"username": _clean(d.get("username")), "display_name": _clean(d.get("name")), "avatar_url": _https(d.get("threads_profile_picture_url"))}
    elif platform == "bluesky":
        r = await client.get(f"{BSKY_PUBLIC}/app.bsky.actor.getProfile", params={"actor": identity})
        if r.status_code == 200:
            d = r.json()
            result = {"username": _clean(d.get("handle")), "display_name": _clean(d.get("displayName")), "avatar_url": _https(d.get("avatar")), "followers": _count(d.get("followersCount"))}
    elif platform == "linkedin":
        r = await client.get(LINKEDIN_USERINFO, headers={"Authorization": f"Bearer {access}"})
        if r.status_code == 200:
            d = r.json()
            result = {"display_name": _clean(d.get("name")), "avatar_url": _https(d.get("picture"))}
    elif platform == "youtube":
        r = await client.get(YOUTUBE_CHANNELS, params={"part": "snippet,statistics", "mine": "true"}, headers={"Authorization": f"Bearer {access}"})
        if r.status_code == 200:
            items = r.json().get("items") or []
            if items:
                snippet, stats = items[0].get("snippet") or {}, items[0].get("statistics") or {}
                thumbs = snippet.get("thumbnails") or {}
                picture = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
                custom = str(snippet.get("customUrl") or "").strip()
                result = {
                    "display_name": _clean(snippet.get("title")), "avatar_url": _https(picture),
                    "followers": None if stats.get("hiddenSubscriberCount") else _count(stats.get("subscriberCount")),
                    # The channel's own short address (youtube.com/@name) is nicer than its id.
                    "profile_url": f"https://www.youtube.com/{custom}" if custom.startswith("@") else None,
                }
    return {key: value for key, value in result.items() if value is not None}


async def refresh_profile(workspace_id: str, platform: str) -> bool:
    """Reads the account's profile and saves it on the connection. Returns True when something was saved."""
    if platform not in SUPPORTED:
        return False
    token = await get_token(workspace_id, platform)
    now = datetime.now(timezone.utc)
    update: dict = {"profile_checked_at": now}
    saved = False
    if token:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                found = await fetch_profile(platform, token, client)
            fields = {k: v for k, v in found.items() if k in ("display_name", "avatar_url", "followers", "profile_url", "username")}
            if fields:
                update.update(fields)
                saved = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Profile read failed for %s/%s: %s", workspace_id, platform, exc)
    await workspace_connections.update_one({"workspace_id": workspace_id, "platform": platform}, {"$set": update})
    return saved


def needs_refresh(account: dict, now: Optional[datetime] = None) -> bool:
    """True for a connected account of a supported platform that has no picture saved and has not been checked in the last day."""
    if account.get("platform") not in SUPPORTED or account.get("avatar_url"):
        return False
    checked = account.get("profile_checked_at")
    if isinstance(checked, str):
        try:
            checked = datetime.fromisoformat(checked)
        except ValueError:
            checked = None
    if checked is None:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - checked > RECHECK_AFTER


_in_flight: set[str] = set()
_tasks: set[asyncio.Task] = set()


def schedule_refresh(workspace_id: str, platforms: list[str]) -> None:
    """Reads the profiles in the background so the accounts list is never held up. One read per account at a time."""
    todo = [p for p in platforms if f"{workspace_id}:{p}" not in _in_flight]
    if not todo or not settings.PROFILE_REFRESH_ENABLED:
        return
    keys = [f"{workspace_id}:{p}" for p in todo]
    _in_flight.update(keys)

    async def run() -> None:
        try:
            await asyncio.gather(*(refresh_profile(workspace_id, p) for p in todo), return_exceptions=True)
        finally:
            _in_flight.difference_update(keys)

    task = asyncio.get_running_loop().create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
