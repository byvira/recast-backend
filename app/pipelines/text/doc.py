"""The rich form of a post: a list of blocks (paragraphs, headings, lists, quotes, dividers) whose text carries a little inline markup.

The post's plain text (`content`) stays the single source of what is published, checked and versioned, exactly as before. The block
form is stored beside it (`content_doc`) so a member can write with structure and formatting, and every save derives the plain text
from it with `to_plain`. A post that was never opened in the rich editor has no block form: `from_plain` builds one from its text,
and nothing about the post changes until it is saved there.

Inline markup in a block's text: `**bold**`, `_italic_` and `[label](https://address)`. Nothing else is interpreted, and no HTML is
ever stored: `to_html` escapes everything and writes only the tags below.
"""

from __future__ import annotations

import html
import re
from typing import Any

VERSION = 1
TYPES = ("paragraph", "heading", "bullets", "numbers", "quote", "divider")
MAX_BLOCKS = 500
MAX_TEXT = 20_000
MAX_ITEMS = 200
MAX_TOTAL = 100_000

_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,2000})\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w_])_(?!_)(.+?)(?<!_)_(?![\w_])", re.S)


class DocError(ValueError):
    """A document that cannot be stored, with the reason in plain words."""


def _need(ok: bool, message: str) -> None:
    if not ok:
        raise DocError(message)


def validate(raw: Any) -> dict[str, Any]:
    """The document in its clean form, or a DocError. Unknown fields are dropped, so nothing unexpected is ever stored."""
    _need(isinstance(raw, dict), "The document must be an object.")
    blocks_raw = raw.get("blocks")
    _need(isinstance(blocks_raw, list) and 1 <= len(blocks_raw) <= MAX_BLOCKS, f"A document has 1 to {MAX_BLOCKS} blocks.")
    seen: set[str] = set()
    total = 0
    blocks: list[dict[str, Any]] = []
    for n, b in enumerate(blocks_raw, 1):
        where = f"Block {n}"
        _need(isinstance(b, dict), f"{where} must be an object.")
        kind = b.get("type")
        _need(kind in TYPES, f"{where} has an unknown type.")
        bid = b.get("id")
        _need(isinstance(bid, str) and bool(_ID.match(bid)), f"{where} needs a short plain id.")
        _need(bid not in seen, f"{where} repeats an id.")
        seen.add(bid)
        block: dict[str, Any] = {"id": bid, "type": kind}
        if kind in ("paragraph", "heading", "quote"):
            text = b.get("text", "")
            _need(isinstance(text, str) and len(text) <= MAX_TEXT, f"{where} text must be at most {MAX_TEXT} characters.")
            block["text"] = text
            total += len(text)
            if kind == "heading":
                level = b.get("level", 2)
                _need(isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 3, f"{where} heading level must be 1 to 3.")
                block["level"] = level
        elif kind in ("bullets", "numbers"):
            items = b.get("items")
            _need(isinstance(items, list) and 1 <= len(items) <= MAX_ITEMS, f"{where} needs 1 to {MAX_ITEMS} items.")
            clean_items: list[str] = []
            for item in items:
                _need(isinstance(item, str) and len(item) <= MAX_TEXT, f"{where} items must be text of at most {MAX_TEXT} characters.")
                clean_items.append(item)
                total += len(item)
            block["items"] = clean_items
        blocks.append(block)
    _need(total <= MAX_TOTAL, f"A document can hold at most {MAX_TOTAL} characters.")
    return {"version": VERSION, "blocks": blocks}


# ── inline markup ────────────────────────────────────────────────────────────────

def strip_inline(text: str) -> str:
    """The text without its markup. A link keeps its label and shows its address after it, since plain text has no links."""
    out = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    out = _BOLD.sub(lambda m: m.group(1), out)
    return _ITALIC.sub(lambda m: m.group(1), out)


def inline_html(text: str) -> str:
    """The text as safe HTML: everything is escaped first, then only bold, italic and links with http or https addresses are written."""
    escaped = html.escape(text, quote=True)
    escaped = _LINK.sub(lambda m: f'<a href="{m.group(2)}" rel="noopener noreferrer">{m.group(1)}</a>', escaped)
    escaped = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    return _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", escaped)


# ── conversions ──────────────────────────────────────────────────────────────────

def to_plain(doc: dict[str, Any]) -> str:
    """The post's own plain-text form: blocks separated by a blank line, lists as lines, markup removed."""
    parts: list[str] = []
    for b in doc.get("blocks", []):
        kind = b["type"]
        if kind in ("paragraph", "heading"):
            text = strip_inline(b.get("text", "")).strip()
            if text:
                parts.append(text)
        elif kind == "quote":
            text = strip_inline(b.get("text", "")).strip()
            if text:
                parts.append(f"“{text}”")
        elif kind == "bullets":
            parts.append("\n".join(f"- {strip_inline(i).strip()}" for i in b.get("items", []) if i.strip()))
        elif kind == "numbers":
            parts.append("\n".join(f"{n}. {strip_inline(i).strip()}" for n, i in enumerate((i for i in b.get("items", []) if i.strip()), 1)))
        elif kind == "divider":
            parts.append("———")
    return "\n\n".join(p for p in parts if p).strip()


def to_html(doc: dict[str, Any]) -> str:
    """The document as HTML for a blog or a newsletter."""
    out: list[str] = []
    for b in doc.get("blocks", []):
        kind = b["type"]
        if kind == "paragraph":
            out.append(f"<p>{inline_html(b.get('text', ''))}</p>")
        elif kind == "heading":
            level = int(b.get("level", 2))
            out.append(f"<h{level}>{inline_html(b.get('text', ''))}</h{level}>")
        elif kind == "quote":
            out.append(f"<blockquote>{inline_html(b.get('text', ''))}</blockquote>")
        elif kind in ("bullets", "numbers"):
            tag = "ul" if kind == "bullets" else "ol"
            out.append(f"<{tag}>" + "".join(f"<li>{inline_html(i)}</li>" for i in b.get("items", [])) + f"</{tag}>")
        elif kind == "divider":
            out.append("<hr>")
    return "\n".join(out)


_BULLET = re.compile(r"^\s*[-*•]\s+(.*\S)\s*$")
_NUMBER = re.compile(r"^\s*\d{1,3}[.)]\s+(.*\S)\s*$")


def from_plain(text: str) -> dict[str, Any]:
    """A block form of an existing plain text, so a post written before the rich editor opens in it unchanged in meaning.

    Blank lines separate blocks. A run of lines that all start with a bullet or a number becomes a list; anything else is a paragraph
    that keeps its own line breaks."""
    blocks: list[dict[str, Any]] = []
    n = 0

    def nid() -> str:
        nonlocal n
        n += 1
        return f"b{n}"

    for chunk in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [l for l in chunk.split("\n") if l.strip()]
        if not lines:
            continue
        if all(_BULLET.match(l) for l in lines):
            blocks.append({"id": nid(), "type": "bullets", "items": [_BULLET.match(l).group(1) for l in lines]})  # type: ignore[union-attr]
        elif all(_NUMBER.match(l) for l in lines):
            blocks.append({"id": nid(), "type": "numbers", "items": [_NUMBER.match(l).group(1) for l in lines]})  # type: ignore[union-attr]
        else:
            blocks.append({"id": nid(), "type": "paragraph", "text": "\n".join(lines)})
    if not blocks:
        blocks.append({"id": "b1", "type": "paragraph", "text": ""})
    return {"version": VERSION, "blocks": blocks}
