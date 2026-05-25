"""Scrape and extract readable article text from any URL using trafilatura."""

import asyncio
import logging
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


def _fetch_and_extract(url: str) -> str | None:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None
    return trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        config=_config,
    )


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

    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(_executor, _fetch_and_extract, url)
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