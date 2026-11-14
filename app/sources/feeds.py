"""Reading a feed: RSS 2.0, Atom and JSON Feed, into items.

Feeds come from anywhere on the internet, so they are treated as hostile: the XML is parsed with `defusedxml` and any document that declares a
DOCTYPE or an ENTITY is refused before it is parsed (the classic way to blow up a parser), only the first `MAX_ITEMS` items are read, and
every text field is turned into plain text and cut. Nothing in a feed is ever followed or run."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from app.sources.items import MAX_TITLE, Item, ReadError, item_id, plain

MAX_ITEMS = 50
MAX_FEED_BYTES = 2 * 1024 * 1024


def _when(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    text = text.strip()
    try:
        moment = parsedate_to_datetime(text)                      # RSS: "Tue, 10 Jun 2025 09:00:00 GMT"
    except (TypeError, ValueError):
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))   # Atom and JSON Feed: ISO 8601
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def looks_like_feed(body: bytes, content_type: str = "") -> bool:
    head = body[:2048].lstrip().lower()
    return head.startswith((b"<?xml", b"<rss", b"<feed", b"<rdf")) or b"<rss" in head or b"<feed" in head or (head.startswith(b"{") and b"jsonfeed" in body[:4096].lower())


def parse(body: bytes) -> tuple[str, list[Item]]:
    """(the feed's title, its newest items). Raises `ReadError` for anything that is not a feed."""
    if len(body) > MAX_FEED_BYTES:
        raise ReadError("too_large", "That feed is larger than a source can read.")
    stripped = body.lstrip()
    if stripped.startswith(b"{"):
        return _json_feed(stripped)
    return _xml_feed(body)


def _xml_feed(body: bytes) -> tuple[str, list[Item]]:
    lowered = body[:8192].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered or b"<!doctype" in body.lower():
        raise ReadError("unsafe_feed", "That feed uses features that are not safe to read, so it was not opened.")
    try:
        from defusedxml import ElementTree as ET

        root = ET.fromstring(body)
    except Exception as exc:  # noqa: BLE001 - any parse failure is "not a feed" to the member
        raise ReadError("not_a_feed", "That address is not a feed Ekavo can read. A feed is an RSS or Atom address.") from exc
    tag = root.tag.split("}")[-1].lower()
    if tag == "rss" or tag == "rdf":
        return _rss(root)
    if tag == "feed":
        return _atom(root)
    raise ReadError("not_a_feed", "That address is not a feed Ekavo can read. A feed is an RSS or Atom address.")


def _local(el) -> str:
    return el.tag.split("}")[-1].lower() if isinstance(el.tag, str) else ""


def _child_text(el, *names: str) -> str:
    for child in el:
        if _local(child) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def _rss(root) -> tuple[str, list[Item]]:
    channel = next((c for c in root if _local(c) == "channel"), root)
    title = plain(_child_text(channel, "title"), MAX_TITLE) or "Feed"
    items: list[Item] = []
    for el in [c for c in channel if _local(c) == "item"] or [c for c in root if _local(c) == "item"]:
        link = _child_text(el, "link")
        name = plain(_child_text(el, "title"), MAX_TITLE)
        body = _child_text(el, "encoded", "description", "summary")
        if not (link or name):
            continue
        items.append(Item(id=item_id(link, _child_text(el, "guid"), name), title=name or link, text=plain(body), link=link, published=_when(_child_text(el, "pubdate", "date")), kind="feed"))
        if len(items) >= MAX_ITEMS:
            break
    return title, items


def _atom(root) -> tuple[str, list[Item]]:
    title = plain(_child_text(root, "title"), MAX_TITLE) or "Feed"
    items: list[Item] = []
    for el in [c for c in root if _local(c) == "entry"]:
        link = ""
        for child in el:
            if _local(child) == "link" and child.attrib.get("rel", "alternate") == "alternate":
                link = child.attrib.get("href", "")
                break
        name = plain(_child_text(el, "title"), MAX_TITLE)
        body = _child_text(el, "content", "summary")
        if not (link or name):
            continue
        items.append(Item(id=item_id(link, _child_text(el, "id"), name), title=name or link, text=plain(body), link=link, published=_when(_child_text(el, "updated", "published")), kind="feed"))
        if len(items) >= MAX_ITEMS:
            break
    return title, items


def _json_feed(body: bytes) -> tuple[str, list[Item]]:
    try:
        data: Any = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ReadError("not_a_feed", "That address is not a feed Ekavo can read.") from exc
    if not isinstance(data, dict) or "jsonfeed" not in str(data.get("version", "")).lower() or not isinstance(data.get("items"), list):
        raise ReadError("not_a_feed", "That address is not a feed Ekavo can read.")
    items: list[Item] = []
    for raw in data["items"]:
        if not isinstance(raw, dict):
            continue
        link = str(raw.get("url") or raw.get("external_url") or "")
        name = plain(str(raw.get("title") or ""), MAX_TITLE)
        if not (link or name):
            continue
        items.append(Item(id=item_id(link, str(raw.get("id") or ""), name), title=name or link, text=plain(str(raw.get("content_text") or raw.get("summary") or raw.get("content_html") or "")), link=link,
                          published=_when(str(raw.get("date_published") or "")), kind="feed"))
        if len(items) >= MAX_ITEMS:
            break
    return plain(str(data.get("title") or ""), MAX_TITLE) or "Feed", items
