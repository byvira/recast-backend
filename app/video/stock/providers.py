"""Stock footage sources: Pixabay and Pexels behind one shape.

Each adapter turns the provider's answer into the same small record, with the author and the licence kept on it, because a clip
that is used must carry who made it and under what terms. Results are cached for 24 hours (Pixabay's own rule, and it keeps the
free allowances), calls are paced per provider, and a rate-limit answer is waited out briefly and then reported plainly.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

PIXABAY_URL = "https://pixabay.com/api/videos/"
PEXELS_URL = "https://api.pexels.com/videos/search"
PEXELS_ONE_URL = "https://api.pexels.com/videos/videos/{id}"
CACHE_HOURS = 24
LICENSES = {
    "pixabay": ("Pixabay Content License", "https://pixabay.com/service/license-summary/"),
    "pexels": ("Pexels License", "https://www.pexels.com/license/"),
}
# calls allowed in a window, a little under each provider's own free limit
_LIMITS = {"pixabay": (80, 60.0), "pexels": (150, 3600.0)}
_seen: dict[str, deque] = {"pixabay": deque(), "pexels": deque()}


class StockError(Exception):
    """A source could not answer; the message is safe to show."""


class RateLimited(StockError):
    def __init__(self, provider: str, retry_after: float):
        super().__init__(f"The {provider.title()} stock library is busy right now. Try again in a minute.")
        self.provider = provider
        self.retry_after = retry_after


def configured() -> list[str]:
    out = []
    if settings.PIXABAY_API_KEY:
        out.append("pixabay")
    if getattr(settings, "PEXELS_API_KEY", ""):
        out.append("pexels")
    return out


async def _pace(provider: str) -> Optional[float]:
    """Seconds to wait before the next call, or None when a call may go now."""
    limit, window = _LIMITS[provider]
    now = time.monotonic()
    q = _seen[provider]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return max(0.1, window - (now - q[0]))
    q.append(now)
    return None


async def _get(provider: str, url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None, attempts: int = 3) -> dict[str, Any]:
    for attempt in range(attempts):
        wait = await _pace(provider)
        if wait is not None:
            if wait > 10:
                raise RateLimited(provider, wait)
            await asyncio.sleep(wait)
            continue
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                res = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("%s request failed: %s", provider, exc)
            if attempt == attempts - 1:
                raise StockError(f"The {provider.title()} stock library could not be reached.") from exc
            await asyncio.sleep(0.5 * (2 ** attempt))
            continue
        if res.status_code == 429:
            retry = float(res.headers.get("Retry-After", "0") or 0) or 2.0 * (2 ** attempt)
            if attempt == attempts - 1 or retry > 10:
                raise RateLimited(provider, retry)
            await asyncio.sleep(retry)
            continue
        if res.status_code in (401, 403):
            raise StockError(f"The {provider.title()} stock library refused the request. Check its key.")
        if res.status_code >= 500:
            if attempt == attempts - 1:
                raise StockError(f"The {provider.title()} stock library is not answering right now.")
            await asyncio.sleep(0.5 * (2 ** attempt))
            continue
        res.raise_for_status()
        return res.json()
    raise StockError(f"The {provider.title()} stock library could not be reached.")


def normalise_pixabay(hit: dict[str, Any]) -> Optional[dict[str, Any]]:
    vids = hit.get("videos") or {}
    pick = next((vids[k] for k in ("medium", "large", "small", "tiny") if vids.get(k, {}).get("url")), None)
    preview = next((vids[k] for k in ("tiny", "small", "medium") if vids.get(k, {}).get("url")), None)
    if not pick or not hit.get("id"):
        return None
    pic = hit.get("picture_id")
    name, lic_url = LICENSES["pixabay"]
    return {
        "provider": "pixabay", "id": str(hit["id"]), "page_url": hit.get("pageURL") or "", "download_url": pick["url"],
        "preview_url": (preview or pick)["url"], "poster_url": f"https://i.vimeocdn.com/video/{pic}_640x360.jpg" if pic else None,
        "width": int(pick.get("width") or 0), "height": int(pick.get("height") or 0), "bytes": int(pick.get("size") or 0),
        "duration_s": float(hit.get("duration") or 0), "author": hit.get("user") or "", "author_url": f"https://pixabay.com/users/{hit.get('user')}-{hit.get('user_id')}/" if hit.get("user") else "",
        "license": name, "license_url": lic_url, "tags": [t.strip() for t in str(hit.get("tags") or "").split(",") if t.strip()],
    }


def normalise_pexels(v: dict[str, Any]) -> Optional[dict[str, Any]]:
    files = [f for f in (v.get("video_files") or []) if f.get("link") and str(f.get("file_type", "")).endswith("mp4")]
    if not files or not v.get("id"):
        return None
    # the best file that is not larger than 1080 on its long side (smaller files upload and render faster)
    fit = [f for f in files if max(int(f.get("width") or 0), int(f.get("height") or 0)) <= 1920] or files
    pick = max(fit, key=lambda f: int(f.get("width") or 0) * int(f.get("height") or 0))
    small = min(files, key=lambda f: int(f.get("width") or 0) * int(f.get("height") or 0))
    name, lic_url = LICENSES["pexels"]
    user = v.get("user") or {}
    return {
        "provider": "pexels", "id": str(v["id"]), "page_url": v.get("url") or "", "download_url": pick["link"], "preview_url": small["link"],
        "poster_url": v.get("image"), "width": int(pick.get("width") or 0), "height": int(pick.get("height") or 0), "bytes": 0,
        "duration_s": float(v.get("duration") or 0), "author": user.get("name") or "", "author_url": user.get("url") or "",
        "license": name, "license_url": lic_url, "tags": [],
    }


async def _fetch(provider: str, query: str, per_page: int) -> list[dict[str, Any]]:
    if provider == "pixabay":
        data = await _get("pixabay", PIXABAY_URL, params={"key": settings.PIXABAY_API_KEY, "q": query[:100], "per_page": max(3, per_page),
                                                         "safesearch": "true", "video_type": "film", "order": "popular"})
        return [c for c in (normalise_pixabay(h) for h in data.get("hits", [])) if c]
    data = await _get("pexels", PEXELS_URL, params={"query": query[:100], "per_page": per_page},
                      headers={"Authorization": settings.PEXELS_API_KEY})
    return [c for c in (normalise_pexels(v) for v in data.get("videos", [])) if c]


def _key(provider: str, query: str, per_page: int) -> str:
    return hashlib.sha1(f"{provider}|{query.strip().casefold()}|{per_page}".encode("utf-8")).hexdigest()


async def search(query: str, *, providers: Optional[list[str]] = None, per_page: int = 12) -> dict[str, Any]:
    """{"clips", "cached", "errors"}: clips from every configured source (cached 24 hours). A source that fails is listed in
    `errors` and the others still answer; when none is configured the answer says so instead of returning nothing silently."""
    query = " ".join(query.split())[:100]
    if not query:
        return {"clips": [], "cached": False, "errors": []}
    use = [p for p in (providers or configured()) if p in configured()]
    if not use:
        return {"clips": [], "cached": False, "errors": ["No stock library is connected yet."], "unconfigured": True}
    col = get_db()["video_stock_cache"]
    clips: list[dict[str, Any]] = []
    errors: list[str] = []
    all_cached = True
    for provider in use:
        key = _key(provider, query, per_page)
        row = await col.find_one({"_id": key})
        if row and row.get("at") and (datetime.now(timezone.utc) - row["at"].replace(tzinfo=timezone.utc)) < timedelta(hours=CACHE_HOURS):
            clips.extend(row["clips"])
            continue
        all_cached = False
        try:
            found = await _fetch(provider, query, per_page)
        except RateLimited as exc:
            errors.append(str(exc))
            continue
        except StockError as exc:
            errors.append(str(exc))
            continue
        await col.update_one({"_id": key}, {"$set": {"clips": found, "provider": provider, "query": query, "at": datetime.now(timezone.utc)}}, upsert=True)
        clips.extend(found)
    return {"clips": clips, "cached": all_cached and bool(clips), "errors": errors}


async def get_one(provider: str, clip_id: str) -> Optional[dict[str, Any]]:
    """One clip looked up by its id at the source, so what is downloaded and what is recorded as its licence come from the source
    and never from the browser."""
    if provider not in configured():
        return None
    try:
        if provider == "pixabay":
            data = await _get("pixabay", PIXABAY_URL, params={"key": settings.PIXABAY_API_KEY, "id": clip_id})
            hits = data.get("hits") or []
            return normalise_pixabay(hits[0]) if hits else None
        data = await _get("pexels", PEXELS_ONE_URL.format(id=clip_id), headers={"Authorization": settings.PEXELS_API_KEY})
        return normalise_pexels(data)
    except StockError:
        return None
