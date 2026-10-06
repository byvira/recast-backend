"""Bluesky does not turn links and hashtags in a post into links by itself: the post names them as "facets", with the position of each
in bytes (UTF-8). Without facets they show as plain text. Built from the text alone, so there is nothing to look up."""
from __future__ import annotations

import re
from typing import Optional

_URL = re.compile(r"(?<![\w@/])https?://[^\s<>\"]+", re.IGNORECASE)
# A bare address such as northwind.example/reviews or recast.app. A known ending is required so a sentence like "e.g." is left alone.
_TLDS = "com|org|net|io|co|ai|dev|app|me|us|uk|in|ca|au|de|fr|nl|se|info|biz|xyz|tv|fm|so|sh|ly|to|social|blog|site|online|tech|studio|example"
_BARE = re.compile(rf"(?<![\w@/.:-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{_TLDS})(?::\d+)?(?:/[^\s<>\"]*)?(?![\w-])", re.IGNORECASE)
_HASHTAG = re.compile(r"(?<![\w&])#(?!️|⃣)([^\s\d\W][\w]*)", re.UNICODE)
_TRAILING = ".,;:!?)]}'\""


def _trim(text: str) -> str:
    """Punctuation that ends a sentence is not part of the link."""
    while text and text[-1] in _TRAILING:
        # A closing bracket belongs to the link when it has its own opening one inside it.
        if text[-1] == ")" and text.count("(") >= text.count(")"):
            break
        text = text[:-1]
    return text


def _byte_range(text: str, start: int, end: int) -> dict:
    return {"byteStart": len(text[:start].encode("utf-8")), "byteEnd": len(text[:end].encode("utf-8"))}


def build_facets(text: str) -> list[dict]:
    """The link and hashtag markup for `text`, in order, never overlapping. Empty when there is nothing to mark."""
    found: list[tuple[int, int, dict]] = []

    def add(start: int, end: int, feature: dict) -> None:
        if end > start and not any(start < e and end > s for s, e, _ in found):
            found.append((start, end, feature))

    for match in _URL.finditer(text):
        link = _trim(match.group(0))
        add(match.start(), match.start() + len(link), {"$type": "app.bsky.richtext.facet#link", "uri": link})
    for match in _BARE.finditer(text):
        link = _trim(match.group(0))
        add(match.start(), match.start() + len(link), {"$type": "app.bsky.richtext.facet#link", "uri": f"https://{link}"})
    for match in _HASHTAG.finditer(text):
        tag = match.group(1)
        if len(tag) > 64:
            continue
        add(match.start(), match.end(), {"$type": "app.bsky.richtext.facet#tag", "tag": tag})

    found.sort(key=lambda item: item[0])
    return [{"index": _byte_range(text, s, e), "features": [feature]} for s, e, feature in found]


def language_tags(options: Optional[dict]) -> list[str]:
    """The languages chosen for the post, up to three, or none."""
    value = (options or {}).get("languages")
    return [str(v) for v in value[:3]] if isinstance(value, list) else []
