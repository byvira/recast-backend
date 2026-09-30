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


def _image_text(doc: dict) -> str:
    parts: list[str] = []
    for slide in doc.get("slides") or []:
        text = (slide or {}).get("text_content") or {}
        headline = _clean(text.get("headline"))
        if headline and headline not in parts:
            parts.append(headline)
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
