"""Content Guard settings: one document that Ops edits, cached in memory so the publish gate and the model wrappers can read
them without a database call."""

from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

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
    "judge_models": ["fast"],
    "profiles": {},
    "terms_by_language": {},
    "shadow_model": "off",
    "shadow_rate": 0.1,
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


def tidy_judges(raw: Any) -> list[str]:
    """The judge names Ops chose, in order, known ones only and each once."""
    from app.agents.content_guard.judges import JUDGES

    out: list[str] = []
    for item in raw or []:
        name = str(item).strip().lower()
        if name in JUDGES and name not in out:
            out.append(name)
    return out or ["fast"]


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


HISTORY_FIELDS = tuple(k for k in DEFAULTS if k not in ("version", "updated_at", "updated_by"))
FIELD_LABELS = {
    "enabled": "Checking is on", "strictness": "How strict", "extra_blocked_terms": "Always block", "allowed_terms": "Always allow",
    "ai_phrases": "Phrases that sound written by a machine", "rewrite_flagged": "Rewrite flagged text once", "media_check": "Check pictures, recordings and video",
    "video_frames": "Look at scenes across each video", "model_check": "When a model also reads the text", "judge_models": "Models that read the text",
    "shadow_model": "Silent second opinion", "shadow_rate": "Share of posts compared", "profiles": "Brand safety profiles", "terms_by_language": "Words for one language",
}


def history_col():
    from app.db.mongo import get_db

    return get_db()["content_safety_history"]


def diff_settings(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """What changed between two saved documents, field by field. A list also says what was added and what was removed."""
    out: list[dict[str, Any]] = []
    for key in HISTORY_FIELDS:
        b, a = before.get(key, DEFAULTS[key]), after.get(key, DEFAULTS[key])
        if a == b:
            continue
        row: dict[str, Any] = {"path": key, "label": FIELD_LABELS.get(key, key), "before": b, "after": a}
        if isinstance(a, list) and isinstance(b, list):
            row["added"] = [x for x in a if x not in b]
            row["removed"] = [x for x in b if x not in a]
        out.append(row)
    return out


async def _write_history(before: dict[str, Any], after: dict[str, Any], *, user_id: str, user_name: Optional[str], reason: str, kind: str) -> None:
    from uuid import uuid4

    diffs = diff_settings(before, after)
    if not diffs:
        return
    try:
        await history_col().insert_one({
            "id": str(uuid4()), "at": datetime.now(timezone.utc), "kind": kind, "by": user_id, "by_name": user_name,
            "reason": " ".join((reason or "").split())[:200], "changes": diffs, "version": after.get("version"),
        })
    except Exception as exc:  # noqa: BLE001 - the settings are saved; only the history row is missing
        import logging

        logging.getLogger(__name__).warning("Could not save a safety settings history row: %s", exc)


async def history(limit: int = 50) -> list[dict[str, Any]]:
    return await history_col().find({}, {"_id": 0}).sort("at", -1).limit(min(max(limit, 1), 200)).to_list(200)


async def restore(history_id: str, *, expected_version: int, user_id: str, user_name: Optional[str] = None) -> dict[str, Any]:
    """Put back the values a past change replaced, as a new change that is itself recorded."""
    row = await history_col().find_one({"id": history_id}, {"_id": 0})
    if not row:
        raise LookupError("That change was not found.")
    back = {c["path"]: c["before"] for c in row["changes"] if c["path"] in HISTORY_FIELDS}
    saved = await save(back, expected_version=expected_version, user_id=user_id, reason="Undid an earlier change", user_name=user_name)
    return saved


class VersionConflict(Exception):
    """The settings changed since the caller read them."""


async def save(changes: dict[str, Any], *, expected_version: int, user_id: str, reason: str = "", user_name: Optional[str] = None) -> dict[str, Any]:
    from app.db.mongo import content_safety

    existing = await load()
    if int(existing.get("version", 0)) != int(expected_version):
        raise VersionConflict()
    updated = {**existing, **changes}
    updated["extra_blocked_terms"] = tidy_terms(updated.get("extra_blocked_terms"))
    updated["allowed_terms"] = tidy_terms(updated.get("allowed_terms"))
    updated["ai_phrases"] = tidy_terms(updated.get("ai_phrases"))
    updated["judge_models"] = tidy_judges(updated.get("judge_models"))
    from app.agents.content_guard.profiles import tidy_overrides

    updated["profiles"] = tidy_overrides(updated.get("profiles"))
    from app.agents.content_guard.term_lists import tidy_by_language

    updated["terms_by_language"] = tidy_by_language(updated.get("terms_by_language"))
    from app.agents.content_guard.judges import JUDGES

    if updated.get("shadow_model") not in JUDGES:
        updated["shadow_model"] = "off"
    updated["shadow_rate"] = min(1.0, max(0.0, float(updated.get("shadow_rate") or 0)))
    updated["version"] = int(existing.get("version", 0)) + 1
    updated["updated_at"] = datetime.now(timezone.utc)
    updated["updated_by"] = user_id
    await content_safety.replace_one({"_id": "config"}, {"_id": "config", **updated}, upsert=True)
    set_cache(updated)
    await _write_history(existing, updated, user_id=user_id, user_name=user_name, reason=reason, kind="save")
    return current()
