"""Brand fonts that are not among the bundled families, fetched once from Google Fonts and kept on disk.

A brand can name any font in the picker. The twelve bundled families cover most brands; for any other name the font is looked up
on Google Fonts (free, open-licensed), downloaded as TrueType once and reused. When the name is not a Google font, the download
fails or the file is not a real font, None is returned and the caller keeps its own fallback (Inter), so a picture is never lost
over a font. The browser editor loads the same family from Google Fonts, so what is seen is what is drawn.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Optional

import httpx

from app.core.logger import logger

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "fonts", "web")
# Font family names are letters, digits and spaces; anything else is never sent anywhere.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{1,39}$")
_CSS_URL = "https://fonts.googleapis.com/css2"
_MAX_FONT_BYTES = 5 * 1024 * 1024
_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
# A name that could not be fetched is not tried again for this long, so a typo never slows every picture.
_RETRY_AFTER_S = 6 * 3600
# TrueType and OpenType files start with one of these.
_FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"OTTO")
_SRC = re.compile(r"font-weight:\s*(\d+);[^}]*?src:\s*url\((https://fonts\.gstatic\.com/[^)]+)\)", re.S)

_lock = threading.RLock()
_failed: dict[str, float] = {}


def valid_name(name: Optional[str]) -> bool:
    return bool(name and _NAME.match(name.strip()))


def _paths(key: str) -> dict[str, str]:
    stem = key.replace(" ", "_")
    return {"regular": os.path.join(_CACHE_DIR, f"{stem}-400.ttf"), "bold": os.path.join(_CACHE_DIR, f"{stem}-700.ttf")}


def _download(client: httpx.Client, url: str) -> Optional[bytes]:
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        body = bytearray()
        for chunk in resp.iter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_FONT_BYTES:
                return None
    data = bytes(body)
    return data if data[:4] in _FONT_MAGIC else None


def font_files(name: Optional[str]) -> Optional[dict[str, str]]:
    """Paths to the regular and bold TrueType files for a Google font family, downloading them the first time. None when the
    name is not valid, is not a Google font, or cannot be fetched right now."""
    if not valid_name(name):
        return None
    family = " ".join(name.strip().split())
    key = family.lower()
    paths = _paths(key)
    if os.path.exists(paths["regular"]):
        return paths if os.path.exists(paths["bold"]) else {"regular": paths["regular"], "bold": paths["regular"]}
    if time.monotonic() - _failed.get(key, -_RETRY_AFTER_S) < _RETRY_AFTER_S:
        return None
    with _lock:
        if os.path.exists(paths["regular"]):
            return font_files(family)
        try:
            # With no browser user agent Google Fonts answers with TrueType files, which the picture renderer reads.
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
                css = client.get(_CSS_URL, params={"family": f"{family}:wght@400;700"})
                if css.status_code != 200:
                    raise ValueError(f"Google Fonts answered {css.status_code}")
                by_weight = {int(w): url for w, url in _SRC.findall(css.text)}
                regular_url = by_weight.get(400) or next(iter(by_weight.values()), None)
                if not regular_url:
                    raise ValueError("no font file in the answer")
                regular = _download(client, regular_url)
                if not regular:
                    raise ValueError("not a font file, or too large")
                bold = _download(client, by_weight[700]) if 700 in by_weight else None
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(paths["regular"], "wb") as fh:
                fh.write(regular)
            if bold:
                with open(paths["bold"], "wb") as fh:
                    fh.write(bold)
            logger.info("Brand font %s fetched from Google Fonts", family)
        except Exception as exc:  # noqa: BLE001 — a missing font must never stop a picture
            _failed[key] = time.monotonic()
            logger.info("Brand font %s could not be fetched, using the default: %s", family, exc)
            return None
    return font_files(family)
