"""Scrape and extract readable article text from any URL using trafilatura."""

import asyncio
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import trafilatura
from trafilatura.settings import use_config

logger = logging.getLogger(__name__)

_config = use_config()
_config.set("DEFAULT", "DOWNLOAD_TIMEOUT", "15")
_config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "100")
_config.set("DEFAULT", "MIN_OUTPUT_SIZE", "100")

_executor = ThreadPoolExecutor(max_workers=4)

# Hostnames that never resolve to a public IP via DNS but still need blocking.
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}


class BlockedURLError(ValueError):
    """Raised when a URL targets a private/internal/cloud-metadata address."""


def _resolve_all_ips(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return []
    return list({info[4][0] for info in infos})


def _guard_public_url(url: str) -> None:
    """Reject URLs that resolve to loopback/private/link-local/reserved ranges.

    Content ingestion fetches whatever URL a user supplies — without this, a
    user could point the scraper at ``http://169.254.169.254/`` (cloud
    metadata), ``http://localhost:6379`` (the app's own Redis), or any other
    internal service reachable from the server.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        raise BlockedURLError(f"URL has no hostname: {url}")
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise BlockedURLError(f"URL host is not allowed: {hostname}")

    ips = _resolve_all_ips(hostname)
    if not ips:
        raise BlockedURLError(f"Could not resolve host: {hostname}")

    for ip_str in ips:
        ip = ipaddress.ip_address(ip_str)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise BlockedURLError(
                f"URL resolves to a non-public address ({ip_str}); "
                "private/internal targets are not allowed"
            )


#: The most of a page that is read, and how many redirects are followed.
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
#: The longest the whole download may take, however slowly the site sends it.
TOTAL_DOWNLOAD_SECONDS = 20


def _safe_download(url: str) -> str | None:
    """The page's text, fetched so that every address it goes to is checked, not only the first.

    A page that redirects to an internal address (or whose name is pointed at one after the first check) would otherwise be read by
    the library's own fetch, which follows redirects without looking. Here each hop is checked before it is requested, the answer
    is cut at MAX_PAGE_BYTES, and the whole download has one deadline."""
    import time

    import httpx

    deadline = time.monotonic() + TOTAL_DOWNLOAD_SECONDS
    current = url
    with httpx.Client(follow_redirects=False, timeout=10.0, headers={"User-Agent": "Mozilla/5.0 (compatible; EkavoReader/1.0)"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _guard_public_url(current)
            with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                    current = str(httpx.URL(current).join(response.headers["location"]))
                    continue
                if response.status_code >= 400:
                    return None
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES or time.monotonic() > deadline:
                        break
                return bytes(body[:MAX_PAGE_BYTES]).decode(response.encoding or "utf-8", errors="replace")
    return None


def _fetch_and_extract(url: str) -> str | None:
    downloaded = _safe_download(url)
    if not downloaded:
        return None
    return trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        config=_config,
    )


def _fetch_and_preview(url: str) -> dict:
    downloaded = _safe_download(url)
    if not downloaded:
        return {"title": None, "snippet": None, "word_count": 0}

    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=True, no_fallback=False, config=_config,
    )
    metadata = trafilatura.extract_metadata(downloaded, default_url=url)

    snippet = None
    word_count = 0
    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        clean = "\n".join(lines)
        word_count = len(clean.split())
        snippet = clean[:220].strip()
        if len(clean) > 220:
            snippet += "…"

    return {
        "title": (metadata.title if metadata else None) or None,
        "snippet": snippet,
        "word_count": word_count,
    }


async def preview_url(url: str) -> dict:
    """Fetch a URL and return {title, snippet, word_count} for a live "what
    will be scraped" preview before the user commits to running the
    pipeline — the frontend's URL input used to show a hardcoded fake
    preview card ("blog.example.com", a canned Discord-community quote) for
    literally any URL typed in, regardless of what was actually there.

    Does not raise when the page isn't extractable — returns nulls instead,
    so the frontend can say "couldn't preview this page" without treating
    it as a hard error. The real scrape_url() (used by generation itself)
    still raises ValueError on the same condition, since that path can't
    proceed without real content.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc or parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL format: {url}")

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _fetch_and_preview, url)
    except BlockedURLError as e:
        raise ValueError(str(e))
    except Exception as e:
        logger.warning("Preview fetch failed for %s: %s", url, e)
        return {"title": None, "snippet": None, "word_count": 0}


async def scrape_url(url: str) -> str:
    """
    Extract readable text from any URL using trafilatura.
    Works for blogs, news, Medium, Substack, documentation, LinkedIn articles.
    Social platform post URLs (Instagram, Facebook, Twitter, TikTok) will return
    empty or garbage — those are handled via OAuth account connections, not scraping.
    YouTube URLs work partially — description only if page is not JS-gated.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid URL format: {url}")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http and https URLs are supported.")

    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(_executor, _fetch_and_extract, url)
    except BlockedURLError as e:
        logger.warning("Blocked SSRF attempt for %s: %s", url, e)
        raise ValueError(str(e))
    except Exception as e:
        logger.error("Trafilatura failed for %s: %s", url, e)
        raise ValueError(f"Could not fetch URL: {url}")

    if not text or len(text.strip()) < 100:
        raise ValueError(
            f"Could not extract readable content from URL: {url}. "
            "The page may require a login, JavaScript rendering, or is behind a paywall. "
            "Try pasting the content directly using Write mode."
        )

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    clean = "\n".join(lines)
    logger.info("Scraped %d chars from %s", len(clean), url)
    return clean