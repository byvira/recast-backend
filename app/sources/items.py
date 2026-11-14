"""What a source gives: items. One shape for every format, so everything after reading (dedupe, relevance, the brief, the writer) is the same."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_SUMMARY = 2000
MAX_TITLE = 200
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|igshid$)", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class ReadError(ValueError):
    """A document that could not be read, in plain words for the member. `code` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Item:
    id: str                         # stable for the same thing seen again (the guid, or a hash of its link)
    title: str
    text: str                       # the summary or the body, as plain text
    link: str = ""
    published: Optional[datetime] = None
    kind: str = "feed"              # feed, page, pdf, docx, csv
    extra: dict = field(default_factory=dict)


def plain(text: str, limit: int = MAX_SUMMARY) -> str:
    """HTML turned into plain text on one line, cut at `limit`."""
    import html

    return _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", text or ""))).strip()[:limit]


def canonical_link(link: str) -> str:
    """The same article under a different tracking tag or a trailing slash is the same link."""
    parts = urlsplit((link or "").strip())
    if not parts.scheme or not parts.netloc:
        return (link or "").strip()
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def item_id(link: str, guid: str = "", title: str = "") -> str:
    """A short id: from the link when there is one (so the same article seen through two feeds is one item), else the guid, else the title."""
    basis = canonical_link(link) or (guid or "").strip() or (title or "").strip().lower()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
