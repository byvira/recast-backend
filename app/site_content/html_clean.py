"""The rich text from the post editor, cut down to what the website shows: known tags and attributes only, links and media from
safe addresses only, and styles limited to alignment and colour. Anything else is removed before it is saved, so pasted scripts,
frames from unknown sites and tracking pixels never reach a visitor."""
from __future__ import annotations

import html
import re
from urllib.parse import urlparse

import lxml.html
from lxml import etree

# tags removed with everything inside them
DROP_WITH_CONTENT = {"script", "style", "noscript", "template", "object", "embed", "applet", "form", "input", "button", "select",
                     "textarea", "svg", "math", "head", "title", "meta", "link", "base"}

ALLOWED: dict[str, set[str]] = {
    **{t: {"style"} for t in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote")},
    "div": {"style", "data-youtube-video"},
    **{t: set() for t in ("strong", "b", "em", "i", "u", "s", "del", "sub", "sup", "code", "br", "hr", "small")},
    "span": {"style"},
    "mark": {"style", "data-color"},
    "pre": set(),
    "ul": {"data-type"},
    "ol": {"start", "data-type"},
    "li": {"data-type", "data-checked", "style"},
    "label": set(),
    "a": {"href", "target", "rel", "title"},
    "img": {"src", "alt", "title", "width", "height", "data-align", "data-width"},
    "figure": {"data-align", "data-width", "data-type"},
    "figcaption": set(),
    "video": {"src", "controls", "poster", "width", "height", "preload", "data-align"},
    "audio": {"src", "controls", "preload"},
    "source": {"src", "type"},
    "iframe": {"src", "width", "height", "allowfullscreen", "title", "allow", "frameborder"},
    "table": set(), "thead": set(), "tbody": set(), "tr": set(),
    "th": {"colspan", "rowspan", "scope", "style"}, "td": {"colspan", "rowspan", "style"},
    "colgroup": set(), "col": {"span", "style"},
}

FRAME_HOSTS = {"www.youtube.com", "youtube.com", "www.youtube-nocookie.com", "player.vimeo.com"}
_STYLE_KEEP = {"text-align": re.compile(r"^(left|center|right|justify)$"),
               "color": re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([\d\s.,%]+\))$"),
               "background-color": re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([\d\s.,%]+\))$"),
               "min-width": re.compile(r"^\d{1,4}px$"), "width": re.compile(r"^\d{1,4}px$")}
_URL_ATTRS = {"href", "src", "poster"}


def _safe_url(tag: str, attr: str, value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if attr == "href":
        if value.startswith(("/", "#")) and not value.startswith("//"):
            return True
        scheme = urlparse(value).scheme.lower()
        return scheme in ("https", "http", "mailto")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if tag == "iframe":
        return parsed.netloc.lower() in FRAME_HOSTS
    return True


def _remove_keep_tail(el) -> None:
    """Takes an element out while the text right after it stays where it was."""
    parent = el.getparent()
    if parent is None:
        return
    if el.tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _clean_style(value: str) -> str:
    kept = []
    for part in value.split(";"):
        if ":" not in part:
            continue
        name, _, val = part.partition(":")
        name, val = name.strip().lower(), val.strip()
        rule = _STYLE_KEEP.get(name)
        if rule and rule.match(val):
            kept.append(f"{name}: {val}")
    return "; ".join(kept)


def clean_html(raw: str) -> str:
    """The editor's HTML with every unknown tag, attribute, unsafe address and style removed."""
    if not raw or not raw.strip():
        return ""
    root = lxml.html.fragment_fromstring(raw, create_parent="div")
    for el in list(root.iter()):
        if el is root:
            continue
        if not isinstance(el.tag, str):  # comments and processing instructions
            _remove_keep_tail(el)
            continue
        tag = el.tag.lower()
        if tag in DROP_WITH_CONTENT:
            el.drop_tree()
            continue
        if tag not in ALLOWED:
            el.drop_tag()  # keeps its text and children
            continue
        allowed = ALLOWED[tag]
        for attr in list(el.attrib):
            name = attr.lower()
            value = el.attrib[attr]
            if name not in allowed or name.startswith("on"):
                del el.attrib[attr]
            elif name in _URL_ATTRS and not _safe_url(tag, name, value):
                del el.attrib[attr]
            elif name == "style":
                style = _clean_style(value)
                if style:
                    el.attrib[attr] = style
                else:
                    del el.attrib[attr]
        if tag in ("img", "video", "audio", "iframe", "source") and "src" not in el.attrib and tag != "video":
            el.drop_tree()
            continue
        if tag == "a":
            if el.attrib.get("target") not in (None, "_blank"):
                del el.attrib["target"]
            if el.attrib.get("target") == "_blank":
                el.attrib["rel"] = "noopener noreferrer"
            elif "rel" in el.attrib:
                del el.attrib["rel"]
        if tag == "iframe":
            el.attrib["allowfullscreen"] = "true"
            if "title" not in el.attrib:
                el.attrib["title"] = "Video"
        if tag in ("video", "audio"):
            el.attrib["controls"] = "true"
            el.attrib["preload"] = "metadata"
    lead = html.escape(root.text) if root.text and root.text.strip() else ""
    return lead + "".join(etree.tostring(child, encoding="unicode", method="html") for child in root)


def plain_text(markup: str) -> str:
    """The words in some HTML, for search, read time and copy checks."""
    if not markup or not markup.strip():
        return ""
    return " ".join(" ".join(lxml.html.fragment_fromstring(markup, create_parent="div").itertext()).split())
