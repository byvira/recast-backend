"""The title, description and picture of a web page, for the link cards a post can carry (Bluesky, LinkedIn). Read from the page's own
Open Graph tags. The address is typed by a member, so it is checked the same way webhook addresses are: https only, public addresses
only, every redirect checked again, and the page is read only up to a small size."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import httpx

from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url

MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 3
_META = re.compile(r"<meta\s+[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class LinkPreview:
    url: str
    title: str
    description: str
    image_url: Optional[str]


def parse_preview(page: str, url: str) -> LinkPreview:
    """The card details from page text. Pure, so it can be tested without a network."""
    found: dict[str, str] = {}
    for tag in _META.findall(page[:MAX_BYTES]):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in found:
            found[key] = html.unescape(attrs["content"]).strip()
    title = found.get("og:title") or found.get("twitter:title") or ""
    if not title:
        match = _TITLE.search(page[:MAX_BYTES])
        title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip() if match else ""
    description = found.get("og:description") or found.get("twitter:description") or found.get("description") or ""
    image = found.get("og:image") or found.get("twitter:image") or ""
    return LinkPreview(
        url=url,
        title=title[:300],
        description=description[:1000],
        image_url=urljoin(url, image) if image else None,
    )


async def fetch_preview(url: str) -> LinkPreview:
    """Reads the page and returns its card details. Raises UnsafeUrl for an address that may not be read, httpx errors for a page that
    cannot be fetched."""
    current = url
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, headers={"User-Agent": "Recast-LinkPreview/1.0"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await assert_safe_url(current)
            response = await client.get(current)
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            response.raise_for_status()
            return parse_preview(response.text[:MAX_BYTES], current)
    raise UnsafeUrl("That address redirects too many times.")
