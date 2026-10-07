"""The text the "3 Fresh Angles" feature works from when the source is an audio
recording or an image instead of a written post.

Angles rewrite existing words. A recording has its script or transcript; an
image has its headline, the description it was made from, and any alt text. This
module turns each asset document into that text (or says there is none), so the
angle prompt never has to know what kind of asset it came from.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

AssetKind = Literal["audio", "image"]

# Shown when an asset has nothing written to build angles from.
NO_TEXT_MESSAGE = {
    "audio": "This recording has no script or transcript yet. Transcribe it first, then try again.",
    "image": "This image has no headline or description to build angles from.",
}


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _audio_text(doc: dict) -> str:
    script = _clean(doc.get("script"))
    if not script:
        words = [_clean(w.get("word")) for w in (doc.get("transcript") or []) if isinstance(w, dict)]
        script = " ".join(w for w in words if w)
    return script


def _words_of(value: Any, depth: int = 0) -> str:
    """The words in a value of any shape: text as it is, a list or object by the text inside it. Slide text has been stored
    as text, as an object (headline, body) and as a list, so none of them may be assumed."""
    if isinstance(value, str):
        return value.strip()
    if depth > 3:
        return ""
    if isinstance(value, dict):
        ordered = [value[k] for k in ("headline", "title", "text", "body", "subtitle", "caption") if k in value]
        return "\n".join(p for p in (_words_of(v, depth + 1) for v in ordered) if p)
    if isinstance(value, (list, tuple)):
        return "\n".join(p for p in (_words_of(v, depth + 1) for v in value) if p)
    return ""


def _image_text(doc: dict) -> str:
    parts: list[str] = []
    slides = doc.get("slides")
    for slide in slides if isinstance(slides, list) else []:
        if not isinstance(slide, dict):
            continue
        text = _words_of(slide.get("text_content"))
        if text and text not in parts:
            parts.append(text)
    for key in ("og_description", "alt_text", "prompt"):
        value = _clean(doc.get(key))
        if value and value not in parts:
            parts.append(value)
    return "\n\n".join(parts)


def text_for_angles(kind: AssetKind, doc: dict) -> Optional[str]:
    """Source text for this asset, led by its title, or None when it has no words."""
    body = _audio_text(doc) if kind == "audio" else _image_text(doc)
    if not body:
        return None
    title = _clean(doc.get("title"))
    return f"{title}\n\n{body}" if title and title not in body else body
