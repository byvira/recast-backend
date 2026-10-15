"""The sound effects pack: twelve short effects made by Ekavo (see generate.py), each with its licence record. They are served from here to
the render worker and the editor's preview, and added to a workspace's library on request so a clip can point at them like any file."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).parent
FILES = HERE / "files"
_ID = re.compile(r"^[a-z0-9_]{1,40}$")


@lru_cache(maxsize=1)
def catalog() -> list[dict[str, Any]]:
    """Every effect with its length, size, checksum and licence."""
    data = json.loads((HERE / "catalog.json").read_text(encoding="utf-8"))
    return data["effects"]


def get(effect_id: str) -> Optional[dict[str, Any]]:
    return next((e for e in catalog() if e["id"] == effect_id), None) if _ID.match(effect_id or "") else None


def path_of(effect_id: str) -> Optional[Path]:
    """The file of an effect, only for an id in the catalog (never a path the caller made up)."""
    return FILES / f"{effect_id}.mp3" if get(effect_id) else None


def public_url(base: str, effect_id: str) -> str:
    return f"{base.rstrip('/')}/api/v1/video-sfx/files/{effect_id}.mp3"
