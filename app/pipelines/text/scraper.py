"""Scrape and extract readable article text from any URL using trafilatura."""

import asyncio
import ipaddress
import logging
import re
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


class UrlFetchError(ValueError):
    """A page that could not be read, with a reason that is clear and safe to show the member as it is."""


#: What the member is told for each way a link can fail. Written for the member: no addresses, codes or library names.
NOT_A_WEB_ADDRESS = "That doesn't look like a web address. Paste the full link, starting with https://"
CANT_OPEN = "We couldn't open that address. Check the link and try again, or paste the text instead."
NEEDS_LOGIN = "That page asks for a login, or doesn't let automated readers in. Open it, copy the text and paste it instead."
NOT_FOUND = "That page wasn't found. Check the link."
SITE_BUSY = "That site isn't answering right now. Try again in a moment, or paste the text instead."
TOO_SLOW = "That site took too long to answer. Try again, or paste the text instead."
IS_A_PDF = "That link is a PDF, which can't be read here. Open it, copy the text and paste it instead."
NOT_AN_ARTICLE = "That link is a {kind}, not an article. Paste the text you want to use instead."
NOT_A_PAGE = "That link isn't a web page we can read. Paste the text you want to use instead."
NO_TEXT = (
    "We couldn't find readable article text on that page. It may need a login or be built so it can't be read automatically. "
    "Paste the text instead."
)

#: The least text a page must give before it is used. The preview and the run use the same number, so a page that previews well can
#: be written from, and one that cannot be is said so before anyone presses run.
MIN_READABLE_CHARS = 100
#: What a run keeps of a page (see normalizer.py); the preview says when a page is longer than this.
RUN_KEEPS_CHARS = 8000


def tidy_address(url: str) -> str:
    """The address as the member most likely meant it: spaces trimmed, and https:// added when only a site name was pasted."""
    url = (url or "").strip()
    if url and "://" not in url and re.match(r"^[\w-]+(\.[\w-]+)+(/|\?|#|$)", url):
        return "https://" + url
    return url


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


def _kind_of(content_type: str) -> str | None:
    """What a non-page link is, in a word, or None when the type is a readable one (or not stated)."""
    if not content_type:
        return None
    if content_type.startswith("application/pdf"):
        return "pdf"
    for prefix, word in (("image/", "picture"), ("video/", "video"), ("audio/", "recording")):
        if content_type.startswith(prefix):
            return word
    readable = ("text/", "html", "xml", "json")
    return "other" if not any(part in content_type for part in readable) else None


def _safe_download(url: str) -> str | None:
    """The page's text, fetched so that every address it goes to is checked, not only the first.

    A page that redirects to an internal address (or whose name is pointed at one after the first check) would otherwise be read by
    the library's own fetch, which follows redirects without looking. Here each hop is checked before it is requested, the answer
    is cut at MAX_PAGE_BYTES, and the whole download has one deadline. A link that cannot be read raises UrlFetchError with the reason."""
    import time

    import httpx

    deadline = time.monotonic() + TOTAL_DOWNLOAD_SECONDS
    current = url
    try:
        with httpx.Client(follow_redirects=False, timeout=10.0, headers={"User-Agent": "Mozilla/5.0 (compatible; EkavoReader/1.0)"}) as client:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    _guard_public_url(current)
                except BlockedURLError as exc:
                    logger.info("A link was refused: %s", exc)
                    raise UrlFetchError(CANT_OPEN) from exc
                with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                        current = str(httpx.URL(current).join(response.headers["location"]))
                        continue
                    status = response.status_code
                    if status in (401, 403, 407, 451):
                        raise UrlFetchError(NEEDS_LOGIN)
                    if status in (404, 410):
                        raise UrlFetchError(NOT_FOUND)
                    if status == 429 or status >= 500:
                        raise UrlFetchError(SITE_BUSY)
                    if status >= 400:
                        raise UrlFetchError(CANT_OPEN)
                    kind = _kind_of(response.headers.get("content-type", "").split(";")[0].strip().lower())
                    if kind == "pdf":
                        raise UrlFetchError(IS_A_PDF)
                    if kind == "other":
                        raise UrlFetchError(NOT_A_PAGE)
                    if kind:
                        raise UrlFetchError(NOT_AN_ARTICLE.format(kind=kind))
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_PAGE_BYTES or time.monotonic() > deadline:
                            break
                    return bytes(body[:MAX_PAGE_BYTES]).decode(response.encoding or "utf-8", errors="replace")
    except httpx.TimeoutException as exc:
        raise UrlFetchError(TOO_SLOW) from exc
    except httpx.HTTPError as exc:
        logger.info("A link could not be reached: %s", exc)
        raise UrlFetchError(CANT_OPEN) from exc
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
        return {"title": None, "snippet": None, "word_count": 0, "readable": False, "problem": NO_TEXT, "will_trim": False}

    text = trafilatura.extract(
        downloaded, include_comments=False, include_tables=True, no_fallback=False, config=_config,
    )
    metadata = trafilatura.extract_metadata(downloaded, default_url=url)

    snippet = None
    word_count = 0
    clean = ""
    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        clean = "\n".join(lines)
        word_count = len(clean.split())
        snippet = clean[:220].strip()
        if len(clean) > 220:
            snippet += "…"

    readable = len(clean) >= MIN_READABLE_CHARS
    return {
        "title": (metadata.title if metadata else None) or None,
        "snippet": snippet,
        "word_count": word_count,
        # The same rule the run uses: a page that previews with only a title cannot be written from, and is said so here.
        "readable": readable,
        "problem": None if readable else NO_TEXT,
        "will_trim": len(clean) > RUN_KEEPS_CHARS,
    }


async def preview_url(url: str) -> dict:
    """Fetch a URL and return {title, snippet, word_count, readable, problem, will_trim} for a live "what will be scraped" preview
    before the user commits to running the pipeline.

    A link that cannot be read does not raise: it returns `readable: false` and a `problem` in plain words, so the page can say why
    next to the field. Only a link that is not a web address at all raises ValueError.
    """
    url = tidy_address(url)
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc or parsed.scheme not in ("http", "https"):
        raise ValueError(NOT_A_WEB_ADDRESS)

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _fetch_and_preview, url)
    except UrlFetchError as exc:
        return {"title": None, "snippet": None, "word_count": 0, "readable": False, "problem": str(exc), "will_trim": False}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Preview fetch failed for %s: %s", url, exc)
        return {"title": None, "snippet": None, "word_count": 0, "readable": False, "problem": CANT_OPEN, "will_trim": False}


async def scrape_url(url: str) -> str:
    """
    Extract readable text from any URL using trafilatura.
    Works for blogs, news, Medium, Substack, documentation, LinkedIn articles.
    Social platform post URLs (Instagram, Facebook, Twitter, TikTok) will return
    empty or garbage — those are handled via OAuth account connections, not scraping.
    YouTube URLs work partially — description only if page is not JS-gated.

    Raises UrlFetchError, whose message is written for the member, when the page cannot be read.
    """
    url = tidy_address(url)
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc or parsed.scheme not in ("http", "https"):
        raise UrlFetchError(NOT_A_WEB_ADDRESS)

    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(_executor, _fetch_and_extract, url)
    except UrlFetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Trafilatura failed for %s: %s", url, exc)
        raise UrlFetchError(CANT_OPEN) from exc

    if not text or len(text.strip()) < MIN_READABLE_CHARS:
        raise UrlFetchError(NO_TEXT)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    clean = "\n".join(lines)
    logger.info("Scraped %d chars from %s", len(clean), url)
    return clean
