"""Content Guard settings: one document that Ops edits, cached in memory so the publish gate and the model wrappers can read
them without a database call."""

from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.agents.content_guard.rules import DEFAULT_AI_PHRASES

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "strictness": "standard",
    "extra_blocked_terms": [],
    "allowed_terms": [],
    "ai_phrases": list(DEFAULT_AI_PHRASES),
    "rewrite_flagged": True,
    "media_check": True,
    "video_frames": False,
    "model_check": "publish",
    "version": 0,
    "updated_at": None,
    "updated_by": None,
}
STRICTNESS = ("standard", "strict")
# off: rules only. risky: a model reads texts that show risk signals. publish: also every post once, just before it is
# scheduled or sent (remembered by content, so a retry costs nothing).
MODEL_CHECKS = ("off", "risky", "publish")
LIST_LIMIT = 300
TERM_LIMIT = 80
CACHE_SECONDS = 30

_cache: dict[str, Any] = {"value": deepcopy(DEFAULTS), "loaded_at": 0.0}


def current() -> dict[str, Any]:
    """The settings the guard is running with right now."""
    return _cache["value"]


def set_cache(value: dict[str, Any]) -> None:
    merged = {**deepcopy(DEFAULTS), **{k: v for k, v in value.items() if k in DEFAULTS}}
    if merged["model_check"] is True:
        merged["model_check"] = "publish"  # saved before the choice had three levels
    elif merged["model_check"] not in MODEL_CHECKS:
        merged["model_check"] = "off"
    _cache["value"] = merged
    _cache["loaded_at"] = time.monotonic()


def reset_cache() -> None:
    set_cache(deepcopy(DEFAULTS))
    _cache["loaded_at"] = 0.0


def tidy_terms(raw: Any) -> list[str]:
    """Lower-cased, trimmed, de-duplicated terms, capped so a bad paste cannot grow the document without limit."""
    seen: list[str] = []
    for item in raw or []:
        term = str(item).strip().lower()[:TERM_LIMIT]
        if term and term not in seen:
            seen.append(term)
        if len(seen) >= LIST_LIMIT:
            break
    return seen


async def load() -> dict[str, Any]:
    from app.db.mongo import content_safety

    doc = await content_safety.find_one({"_id": "config"}) or {}
    doc.pop("_id", None)
    set_cache(doc)
    return current()


async def ensure_fresh() -> dict[str, Any]:
    """Reload the settings when the cached copy is older than CACHE_SECONDS. A database problem keeps the last good copy."""
    if time.monotonic() - _cache["loaded_at"] > CACHE_SECONDS:
        try:
            return await load()
        except Exception:
            _cache["loaded_at"] = time.monotonic()
    return current()


class VersionConflict(Exception):
    """The settings changed since the caller read them."""


async def save(changes: dict[str, Any], *, expected_version: int, user_id: str) -> dict[str, Any]:
    from app.db.mongo import content_safety

    existing = await load()
    if int(existing.get("version", 0)) != int(expected_version):
        raise VersionConflict()
    updated = {**existing, **changes}
    updated["extra_blocked_terms"] = tidy_terms(updated.get("extra_blocked_terms"))
    updated["allowed_terms"] = tidy_terms(updated.get("allowed_terms"))
    updated["ai_phrases"] = tidy_terms(updated.get("ai_phrases"))
    updated["version"] = int(existing.get("version", 0)) + 1
    updated["updated_at"] = datetime.now(timezone.utc)
    updated["updated_by"] = user_id
    await content_safety.replace_one({"_id": "config"}, {"_id": "config", **updated}, upsert=True)
    set_cache(updated)
    return current()
