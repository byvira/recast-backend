"""Fetch audio from a link the member pastes: a direct audio file, or a
podcast RSS feed to pick an episode from.

This is a server making a request to an address a user typed, so it is
guarded the same way the article scraper is (app.pipelines.text.scraper):
public addresses only, checked again on every redirect, with a hard size cap
and a short timeout. Video sites are not fetched at all.
"""

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx
from pydantic import BaseModel

from app.pipelines.text.scraper import BlockedURLError, _guard_public_url

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
_MAX_REDIRECTS = 4
_MAX_FEED_BYTES = 2 * 1024 * 1024
_MAX_EPISODES = 25

# Sites whose pages are videos, not audio files. Downloading from them
# breaks their terms, so they are refused with a clear message instead.
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "instagram.com", "facebook.com", "x.com", "twitter.com")

_EXT_TO_MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".webm": "audio/webm"}
_MIME_ALIASES = {
    "audio/mp3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/aac": "audio/mp4",
}
_ALLOWED_MIMES = {"audio/mpeg", "audio/wav", "audio/mp4", "audio/webm"}


class LinkFetchError(ValueError):
    """Carries a message that is safe and useful to show the member."""


class FeedEpisode(BaseModel):
    title: str
    audio_url: str
    published_at: Optional[datetime] = None
    duration_s: Optional[float] = None


class FeedListing(BaseModel):
    feed_title: str
    episodes: list[FeedEpisode]


def is_video_link(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _VIDEO_HOSTS)


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LinkFetchError("Paste a full link that starts with http:// or https://.")
    if is_video_link(url):
        raise LinkFetchError(
            "Links to video sites can't be imported. Download the audio yourself and use Raw Upload."
        )


async def _guard(url: str) -> None:
    _check_url(url)
    try:
        await asyncio.get_running_loop().run_in_executor(None, _guard_public_url, url)
    except BlockedURLError:
        raise LinkFetchError("That address can't be reached from here. Use a public link.")


async def _get_bytes(url: str, max_bytes: int) -> tuple[bytes, httpx.Headers, str]:
    """GET with manual, re-checked redirects and a hard size cap. Returns the
    body, its headers and the final URL."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=_TIMEOUT) as client:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            await _guard(current)
            try:
                async with client.stream("GET", current, headers={"User-Agent": "RecastImporter/1.0"}) as res:
                    if res.status_code in (301, 302, 303, 307, 308):
                        location = res.headers.get("location")
                        if not location:
                            raise LinkFetchError("That link redirected nowhere.")
                        current = urljoin(current, location)
                        continue
                    if res.status_code >= 400:
                        raise LinkFetchError(f"That link answered with an error ({res.status_code}).")
                    declared = res.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise LinkFetchError(f"That file is larger than the {max_bytes // (1024 * 1024)}MB limit.")
                    body = bytearray()
                    async for chunk in res.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise LinkFetchError(f"That file is larger than the {max_bytes // (1024 * 1024)}MB limit.")
                    return bytes(body), res.headers, current
            except httpx.TimeoutException:
                raise LinkFetchError("That link took too long to answer.")
            except httpx.HTTPError as exc:
                logger.info("Link fetch failed for %s: %s", current, exc)
                raise LinkFetchError("Couldn't reach that link.")
        raise LinkFetchError("That link redirected too many times.")


def _filename_from(url: str) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "imported-audio"
    return name[:120]


def _normalize_mime(header_mime: str, url: str) -> Optional[str]:
    mime = (header_mime or "").split(";")[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    if mime in _ALLOWED_MIMES:
        return mime
    # Hosts often send a generic type; the file extension is then the honest hint.
    if mime in ("", "application/octet-stream", "binary/octet-stream"):
        ext = "." + urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path else ""
        return _EXT_TO_MIME.get(ext)
    return None


async def fetch_audio(url: str, max_bytes: int) -> tuple[bytes, str, str]:
    """Returns (bytes, mime_type, filename) of a direct audio file link."""
    body, headers, final_url = await _get_bytes(url.strip(), max_bytes)
    mime = _normalize_mime(headers.get("content-type", ""), final_url)
    if not mime:
        raise LinkFetchError("That link isn't an audio file. It should end in .mp3, .wav or .m4a.")
    if not body:
        raise LinkFetchError("That link returned an empty file.")
    return body, mime, _filename_from(final_url)


def _parse_duration(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    parts = text.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def parse_feed(xml_bytes: bytes) -> FeedListing:
    """Reads the episodes out of a podcast RSS feed. Documents that declare
    entities are refused outright (expansion attacks)."""
    head = xml_bytes[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in xml_bytes[:65536].lower():
        raise LinkFetchError("That feed isn't a normal podcast feed.")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise LinkFetchError("That link isn't a podcast feed.")

    channel = root.find("channel")
    if channel is None:
        raise LinkFetchError("That link isn't a podcast feed.")
    feed_title = (channel.findtext("title") or "Podcast").strip()

    itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    episodes: list[FeedEpisode] = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue
        audio_url = enclosure.get("url", "").strip()
        etype = (enclosure.get("type") or "").lower()
        if not (etype.startswith("audio/") or _normalize_mime("", audio_url)):
            continue
        published: Optional[datetime] = None
        pub_text = item.findtext("pubDate")
        if pub_text:
            try:
                published = parsedate_to_datetime(pub_text)
            except (TypeError, ValueError):
                published = None
        episodes.append(FeedEpisode(
            title=(item.findtext("title") or "Untitled episode").strip(),
            audio_url=audio_url,
            published_at=published,
            duration_s=_parse_duration(item.findtext(itunes + "duration")),
        ))
        if len(episodes) >= _MAX_EPISODES:
            break

    if not episodes:
        raise LinkFetchError("That feed has no audio episodes in it.")
    return FeedListing(feed_title=feed_title, episodes=episodes)


async def list_feed_episodes(url: str) -> FeedListing:
    body, _, _ = await _get_bytes(url.strip(), _MAX_FEED_BYTES)
    return parse_feed(body)

