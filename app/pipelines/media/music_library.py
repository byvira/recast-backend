"""Curated free music pack — real Jamendo tracks, real CC BY attribution.

One-time ingest (see scripts/seed_music_library.py), not a per-request call:
fetches a batch of tracks from Jamendo's public API, re-hosts each one's real
audio bytes on Cloudinary (so playback never depends on Jamendo staying up),
and records the real artist + license per track.

Real finding, live-verified against Jamendo's actual API and docs
(https://developer.jamendo.com/v3.0/tracks) before building this: there is
no "CC0 only" filter — the only documented license params are `ccnc`/`ccnd`/
`ccsa` (Non-Commercial / No-Derivatives / Share-Alike), and every Jamendo
track requires at least attribution (CC BY). Restricting all three to 0
keeps only the least-restrictive variant (commercial use + derivatives
allowed, no share-alike), which is what a podcast music bed needs, but
attribution is unavoidable — this pack is CC BY, not CC0. Callers must show
each track's real artist + license somewhere the listener can see it (see
`AudioSourcePanel`'s music tab), a real legal requirement, not decoration.

Jamendo's API needs a free client_id (https://devportal.jamendo.com/, no
card) — see app.core.config.settings.JAMENDO_CLIENT_ID. Never guess or
substitute an undisclosed shared key here; if it's missing, fail clearly.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import httpx

from app.core.config import settings
from app.db.mongo import media_assets, music_library_tracks
from app.models.audio_asset import MusicLibraryTrack
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.shared.storage import ContentType, upload_file_detailed

logger = logging.getLogger(__name__)

JAMENDO_TRACKS_URL = "https://api.jamendo.com/v3.0/tracks/"
SYSTEM_USER_ID = "system_music_library"
DEFAULT_LICENSE_URL = "https://creativecommons.org/licenses/by/3.0/"


class MusicLibraryError(Exception):
    pass


def _license_name_from_url(url: str) -> str:
    """`.../licenses/by-nc/3.0/` -> 'CC BY-NC 3.0'. Falls back to a plain
    'CC BY' when the URL doesn't parse — every Jamendo track is at least
    that, per this module's own docstring."""
    match = re.search(r"/licenses/([a-z-]+)/(\d+\.\d+)", url or "")
    if not match:
        return "CC BY"
    variant, version = match.groups()
    return f"CC {variant.upper()} {version}"


#  Live-verified: with the ccnc/ccnd/ccsa filters applied, Jamendo's own
#  /tracks endpoint returns results_count=0 for some (limit, offset)
#  combinations and real, correct results for others — reproducible,
#  non-monotonic, not explained by anything in Jamendo's own docs, and not
#  even consistently tied to the value itself (offset=0 empty, offset=25
#  real, offset=50 empty again). Rather than chase a third-party API's own
#  undocumented internal behavior further, _fetch_page below treats an
#  empty page as possibly transient: one retry, then move on rather than
#  assuming "end of catalog."
_PAGE_SIZE = 25
_MAX_PAGES = 12
_MAX_CONCURRENT_INGESTS = 8


async def _fetch_page(client: httpx.AsyncClient, offset: int) -> list[dict]:
    for attempt in range(2):
        response = await client.get(
            JAMENDO_TRACKS_URL,
            params={
                "client_id": settings.JAMENDO_CLIENT_ID,
                "format": "json",
                "limit": _PAGE_SIZE,
                "offset": offset,
                "ccnc": 0,
                "ccnd": 0,
                "ccsa": 0,
                "include": "musicinfo",
            },
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        if results:
            return results
        if attempt == 0:
            await asyncio.sleep(0.5)
    return []


async def _fetch_raw_tracks(limit: int) -> list[dict]:
    collected: list[dict] = []
    seen_ids: set[str] = set()
    async with httpx.AsyncClient(timeout=30.0) as client:
        for page_num in range(_MAX_PAGES):
            if len(collected) >= limit:
                break
            page = await _fetch_page(client, offset=page_num * _PAGE_SIZE)
            for track in page:
                track_id = str(track.get("id"))
                if track_id not in seen_ids:
                    seen_ids.add(track_id)
                    collected.append(track)
    return collected[:limit]


async def _ingest_one(track: dict, semaphore: asyncio.Semaphore) -> Optional[MusicLibraryTrack]:
    source_track_id = str(track.get("id"))
    existing = await music_library_tracks.find_one({"source_track_id": source_track_id})
    if existing:
        return MusicLibraryTrack(**existing)

    download_url = track.get("audiodownload") or track.get("audio")
    if not download_url:
        logger.warning("Jamendo track %s has no downloadable audio URL, skipping.", source_track_id)
        return None

    async with semaphore:
        async with httpx.AsyncClient(timeout=60.0) as client:
            audio_response = await client.get(download_url)
            audio_response.raise_for_status()
            audio_bytes = audio_response.content
        upload = await upload_file_detailed(audio_bytes, ContentType.AUDIO, SYSTEM_USER_ID)

    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()),
        workspace_id="__system__",
        kind=MediaKind.AUDIO,
        url=upload["url"],
        mime_type="audio/mpeg",
        source=MediaSource.LIBRARY,
        created_by=SYSTEM_USER_ID,
        created_at=now,
        size_bytes=upload.get("bytes") or len(audio_bytes),
        duration_s=upload.get("duration_s"),
    )
    await media_assets.insert_one(media.model_dump())

    license_url = track.get("license_ccurl") or DEFAULT_LICENSE_URL
    entry = MusicLibraryTrack(
        id=uuid4().hex,
        name=track.get("name", "Untitled"),
        media_id=media.id,
        artist=track.get("artist_name", "Unknown artist"),
        source="jamendo",
        source_track_id=source_track_id,
        license_name=_license_name_from_url(license_url),
        license_url=license_url,
        created_at=now,
    )
    await music_library_tracks.insert_one(entry.model_dump())
    return entry


async def fetch_curated_tracks(limit: int = 15) -> list[MusicLibraryTrack]:
    """Real Jamendo API calls (paginated) + real Cloudinary re-hosting
    (parallelized, bounded) for up to `limit` tracks, restricted to the
    least-restrictive real license Jamendo offers (commercial + derivative
    use allowed). Idempotent: a track already ingested (matched by its
    Jamendo id) is skipped, not re-downloaded."""
    if not settings.JAMENDO_CLIENT_ID:
        raise MusicLibraryError(
            "JAMENDO_CLIENT_ID is not set. Get a free client_id at "
            "https://devportal.jamendo.com/ and add it before running this."
        )

    raw_tracks = await _fetch_raw_tracks(limit)
    if not raw_tracks:
        raise MusicLibraryError("Jamendo returned zero tracks for this query.")

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_INGESTS)
    results = await asyncio.gather(*[_ingest_one(t, semaphore) for t in raw_tracks])
    return [r for r in results if r is not None]


async def list_library_tracks() -> list[MusicLibraryTrack]:
    docs = await music_library_tracks.find({}).to_list(length=100)
    return [MusicLibraryTrack(**d) for d in docs]
