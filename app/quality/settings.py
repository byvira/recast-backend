"""Quality settings that Ops edits: one saved document, read through a short in-memory cache so the checks never wait on the database.

What is saved wins; anything not saved takes its starting value from `defaults.py`, which is also what "Reset" goes back to.
Every save is checked against a range, needs a reason, and writes one history row holding the old and new value of each field, so a
change can be undone. The safety floor (hate, violence, self-harm and assistant-leftover patterns) is not part of this document.
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db
from app.quality import defaults as D

CACHE_SECONDS = 30
LIST_LIMIT = 100
TERM_LIMIT = 60
PATTERN_LIMIT = 80
REASON_MIN = 4
REASON_MAX = 200
HISTORY_LIMIT = 200

# (low, high, whole number?) for every number Ops may change.
NUMBER_RANGES: dict[str, tuple[float, float, bool]] = {
    "min_words": (0, 2000, True),
    "readability": (0, 100, True),
    "trust.min_decided": (1, 200, True),
    "trust.recommend_min_decisions": (5, 500, True),
    "trust.recommend_min_agreement": (50, 100, True),
    "trust.start_bar": (0, 100, True),
    "trust.weight_untouched": (0, 100, True),
    "trust.weight_kept": (0, 100, True),
    "trust.weight_quality": (0, 100, True),
    "trust.weight_delivered": (0, 100, True),
    "patterns.min_per_side": (2, 50, True),
    "patterns.min_ratio": (1.1, 10, False),
    "patterns.min_samples": (3, 500, True),
    "patterns.max_samples": (50, 1000, True),
}
LISTS = ("cta_markers", "weasel_words", "passive_patterns")
WEIGHTS = ("weight_untouched", "weight_kept", "weight_quality", "weight_delivered")

LABELS = {
    "min_words": "Shortest post",
    "readability": "Reading ease needed",
    "cta_markers": "Call to action words",
    "weasel_words": "Filler words",
    "passive_patterns": "Passive voice patterns",
    "trust.min_decided": "Decided drafts before a score counts",
    "trust.recommend_min_decisions": "Decisions before auto-publish is suggested",
    "trust.recommend_min_agreement": "Agreement before it is suggested",
    "trust.start_bar": "Starting score bar",
    "trust.weight_untouched": "Weight: approved untouched",
    "trust.weight_kept": "Weight: not rejected",
    "trust.weight_quality": "Weight: passed the checks",
    "trust.weight_delivered": "Weight: delivered",
    "patterns.min_per_side": "Posts needed on each side",
    "patterns.min_ratio": "Times better needed",
    "patterns.min_samples": "Posts needed before learning starts",
    "patterns.max_samples": "Most recent posts looked at",
}


class SettingsError(ValueError):
    """A value that cannot be saved; the message is shown to the person as it is."""


class VersionConflict(Exception):
    """The settings changed since the caller read them."""


def defaults() -> dict[str, Any]:
    return {
        "min_words": dict(D.MIN_WORDS),
        "readability": dict(D.READABILITY),
        "cta_markers": list(D.CTA_MARKERS),
        "weasel_words": list(D.WEASEL_WORDS),
        "passive_patterns": list(D.PASSIVE_PATTERNS),
        "trust": {k: v for k, v in D.TRUST.items()},
        "patterns": {k: v for k, v in D.PATTERNS.items()},
    }


_cache: dict[str, Any] = {"value": defaults(), "version": 0, "loaded_at": 0.0}


def _merge(saved: dict[str, Any]) -> dict[str, Any]:
    """Saved values over the starting ones; a field that is missing or no longer valid keeps its starting value."""
    out = defaults()
    for group in ("min_words", "readability"):
        for platform, value in ((saved.get(group) or {}).items()):
            if platform in out[group]:
                try:
                    out[group][platform] = _number(f"{group}", value)
                except SettingsError:
                    pass
    for name in LISTS:
        if isinstance(saved.get(name), list):
            out[name] = [str(x) for x in saved[name]]
    for group in ("trust", "patterns"):
        for key, value in ((saved.get(group) or {}).items()):
            if key in out[group]:
                try:
                    out[group][key] = _number(f"{group}.{key}", value)
                except SettingsError:
                    pass
    return out


# Set for the length of one "try a text" request only, so unsaved values can be tried without touching what other requests read.
_trial: ContextVar[Optional[dict[str, Any]]] = ContextVar("quality_trial", default=None)


@contextmanager
def trying(values: dict[str, Any]):
    token = _trial.set(values)
    try:
        yield
    finally:
        _trial.reset(token)


def current() -> dict[str, Any]:
    trial = _trial.get()
    return trial if trial is not None else _cache["value"]


def version() -> int:
    return int(_cache["version"])


def set_cache(saved: dict[str, Any], ver: int = 0) -> None:
    _cache["value"] = _merge(saved or {})
    _cache["version"] = ver
    _cache["loaded_at"] = time.monotonic()


def reset_cache() -> None:
    set_cache({}, 0)
    _cache["loaded_at"] = 0.0


def _col():
    return get_db()["quality_settings"]


def _history():
    return get_db()["quality_history"]


async def load() -> dict[str, Any]:
    doc = await _col().find_one({"_id": "config"}) or {}
    set_cache(doc.get("values") or {}, int(doc.get("version", 0)))
    return current()


async def ensure_fresh() -> dict[str, Any]:
    """Reload when the cached copy is old. A database problem keeps the last good copy, so a check never fails because of it."""
    if time.monotonic() - float(_cache["loaded_at"]) > CACHE_SECONDS:
        try:
            await load()
        except Exception:  # noqa: BLE001
            _cache["loaded_at"] = time.monotonic()
    return current()


# ── Readers used by the checks ────────────────────────────────────────────────────────────────────────────────────────────────────────
def min_words(platform: str) -> int:
    return int(current()["min_words"].get(platform, D.MIN_WORDS_FALLBACK))


def min_words_map() -> dict[str, int]:
    return dict(current()["min_words"])


def readability_bar(platform: str) -> int:
    return int(current()["readability"].get(platform, D.READABILITY_FALLBACK))


def cta_markers() -> list[str]:
    return [m.lower() for m in current()["cta_markers"]]


def weasel_words() -> list[str]:
    return list(current()["weasel_words"])


def passive_patterns() -> list[str]:
    return list(current()["passive_patterns"])


def trust() -> dict[str, float]:
    return current()["trust"]


def patterns() -> dict[str, float]:
    return current()["patterns"]


def trust_weights() -> dict[str, float]:
    t = trust()
    return {"untouched": t["weight_untouched"] / 100, "kept": t["weight_kept"] / 100,
            "quality": t["weight_quality"] / 100, "delivered": t["weight_delivered"] / 100}


# ── Checking what Ops sends ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _number(path: str, value: Any) -> float | int:
    key = path if path in NUMBER_RANGES else path.split(".")[0]
    low, high, whole = NUMBER_RANGES[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{LABELS.get(key, path)} must be a number.")
    if value != value or value in (float("inf"), float("-inf")):
        raise SettingsError(f"{LABELS.get(key, path)} must be a number.")
    if whole:
        if abs(value - round(value)) > 1e-9:
            raise SettingsError(f"{LABELS.get(key, path)} must be a whole number.")
        value = int(round(value))
    else:
        value = round(float(value), 2)
    if value < low or value > high:
        shown_low, shown_high = (int(low), int(high)) if whole else (low, high)
        raise SettingsError(f"{LABELS.get(key, path)} must be between {shown_low} and {shown_high}.")
    return value


def tidy_list(name: str, raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise SettingsError(f"{LABELS[name]} must be a list.")
    limit = PATTERN_LIMIT if name == "passive_patterns" else TERM_LIMIT
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if name != "passive_patterns":
            text = text.lower()
        if not text:
            continue
        if len(text) > limit:
            raise SettingsError(f"An entry in {LABELS[name].lower()} is longer than {limit} characters.")
        if name == "passive_patterns":
            _check_pattern(text)
        if text not in out:
            out.append(text)
    if len(out) > LIST_LIMIT:
        raise SettingsError(f"{LABELS[name]} can hold up to {LIST_LIMIT} entries.")
    if name == "cta_markers" and not out:
        raise SettingsError("Keep at least one call to action word, or every post would be told it has none.")
    return out


_NESTED_QUANTIFIER = re.compile(r"\)[*+{]")


def _check_pattern(text: str) -> None:
    try:
        re.compile(text)
    except re.error:
        raise SettingsError(f"“{text[:30]}” is not a valid pattern.")
    if _NESTED_QUANTIFIER.search(text):
        raise SettingsError(f"“{text[:30]}” repeats a group, which can make the check very slow. Use a simpler pattern.")


def clean_changes(changes: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """The changes in their tidy form, merged with `base` to check rules that span fields (the weights add up to 100)."""
    out: dict[str, Any] = {}
    for group in ("min_words", "readability"):
        if group in changes:
            if not isinstance(changes[group], dict):
                raise SettingsError(f"{LABELS[group]} is not in the right form.")
            out[group] = {}
            for platform, value in changes[group].items():
                if platform not in D.PLATFORMS:
                    raise SettingsError(f"“{str(platform)[:30]}” is not a platform.")
                out[group][platform] = _number(group, value)
    for name in LISTS:
        if name in changes:
            out[name] = tidy_list(name, changes[name])
    for group in ("trust", "patterns"):
        if group in changes:
            if not isinstance(changes[group], dict):
                raise SettingsError("These numbers are not in the right form.")
            out[group] = {}
            for key, value in changes[group].items():
                if key not in base[group]:
                    raise SettingsError(f"“{str(key)[:30]}” is not a setting.")
                out[group][key] = _number(f"{group}.{key}", value)
    unknown = set(changes) - {"min_words", "readability", "trust", "patterns", *LISTS}
    if unknown:
        raise SettingsError(f"“{sorted(unknown)[0][:30]}” is not a setting.")
    if "trust" in out:
        merged = {**base["trust"], **out["trust"]}
        if sum(merged[w] for w in WEIGHTS) != 100:
            raise SettingsError(f"The four weights must add up to 100. They add up to {int(sum(merged[w] for w in WEIGHTS))}.")
    return out


def touches_trust(changes: dict[str, Any]) -> bool:
    return "trust" in changes


def _flatten(values: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for group in ("min_words", "readability", "trust", "patterns"):
        for key, value in values[group].items():
            flat[f"{group}.{key}"] = value
    for name in LISTS:
        flat[name] = list(values[name])
    return flat


def _apply(values: dict[str, Any], path: str, value: Any) -> None:
    if "." in path and path.split(".")[0] in ("min_words", "readability", "trust", "patterns"):
        group, key = path.split(".", 1)
        values[group][key] = value
    else:
        values[path] = value


def edited_paths(values: Optional[dict[str, Any]] = None) -> list[str]:
    """Every field whose value differs from its starting value."""
    now, base = _flatten(values or current()), _flatten(defaults())
    return sorted(p for p in now if now[p] != base[p])


def describe(path: str) -> str:
    if path in LISTS:
        return LABELS[path]
    group, key = path.split(".", 1)
    if group in ("min_words", "readability"):
        return f"{LABELS[group]}, {key}"
    return LABELS[path]


def public() -> dict[str, Any]:
    """What the screen shows: the live values, the starting values, the ranges and which fields were edited."""
    return {
        "version": version(),
        "values": deepcopy(current()),
        "defaults": defaults(),
        "edited": edited_paths(),
        "ranges": {k: {"low": v[0], "high": v[1], "whole": v[2]} for k, v in NUMBER_RANGES.items()},
        "platforms": list(D.PLATFORMS),
        "limits": {"list": LIST_LIMIT, "term": TERM_LIMIT, "pattern": PATTERN_LIMIT, "reason_min": REASON_MIN},
    }


# ── Saving, history and undo ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _history_row(*, kind: str, by: str, by_name: Optional[str], reason: str, diffs: list[dict[str, Any]], ver: int) -> dict[str, Any]:
    return {"id": str(uuid4()), "at": datetime.now(timezone.utc), "kind": kind, "by": by, "by_name": by_name,
            "reason": reason, "changes": diffs, "version": ver}


def _tidy_reason(reason: Any, *, required: bool = True) -> str:
    text = " ".join(str(reason or "").split())[:REASON_MAX]
    if required and len(text) < REASON_MIN:
        raise SettingsError("Say in a few words why you are changing this.")
    return text


async def _write(new_values: dict[str, Any], expected_version: int, history: dict[str, Any]) -> None:
    """Save only if nobody saved in between, then keep the history row. The version check is in the database write itself."""
    ver = expected_version + 1
    if expected_version == 0:
        try:
            await _col().update_one({"_id": "config", "version": {"$exists": False}}, {"$set": {"values": new_values, "version": ver, "updated_at": history["at"], "updated_by": history["by"]}}, upsert=True)
        except Exception as exc:  # noqa: BLE001 - the document already exists, so another save got in first
            raise VersionConflict() from exc
    else:
        result = await _col().update_one({"_id": "config", "version": expected_version}, {"$set": {"values": new_values, "version": ver, "updated_at": history["at"], "updated_by": history["by"]}})
        if result.matched_count == 0:
            raise VersionConflict()
    await _history().insert_one(history)
    set_cache(new_values, ver)


async def save(changes: dict[str, Any], *, reason: Any, expected_version: int, user_id: str, user_name: Optional[str] = None) -> dict[str, Any]:
    existing = await load()
    if int(expected_version) != version():
        raise VersionConflict()
    tidy = clean_changes(changes or {}, existing)
    why = _tidy_reason(reason)
    new_values = deepcopy(existing)
    before_flat = _flatten(existing)
    for group in ("min_words", "readability", "trust", "patterns"):
        for key, value in (tidy.get(group) or {}).items():
            new_values[group][key] = value
    for name in LISTS:
        if name in tidy:
            new_values[name] = tidy[name]
    after_flat = _flatten(new_values)
    diffs = [{"path": p, "label": describe(p), "before": before_flat[p], "after": after_flat[p]} for p in after_flat if after_flat[p] != before_flat[p]]
    if not diffs:
        raise SettingsError("Nothing was changed.")
    await _write(new_values, version(), _history_row(kind="save", by=user_id, by_name=user_name, reason=why, diffs=diffs, ver=version() + 1))
    return current()


async def reset_all(*, reason: Any, expected_version: int, user_id: str, user_name: Optional[str] = None) -> dict[str, Any]:
    """Every field back to its starting value, as one change that can itself be undone."""
    existing = await load()
    if int(expected_version) != version():
        raise VersionConflict()
    why = _tidy_reason(reason)
    base = defaults()
    before_flat, after_flat = _flatten(existing), _flatten(base)
    diffs = [{"path": p, "label": describe(p), "before": before_flat[p], "after": after_flat[p]} for p in after_flat if after_flat[p] != before_flat[p]]
    if not diffs:
        raise SettingsError("Everything is already at its starting value.")
    await _write(base, version(), _history_row(kind="reset", by=user_id, by_name=user_name, reason=why, diffs=diffs, ver=version() + 1))
    return current()


async def history(limit: int = 50, before: Optional[datetime] = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if before:
        query["at"] = {"$lt": before}
    rows = await _history().find(query, {"_id": 0}).sort("at", -1).limit(min(max(limit, 1), HISTORY_LIMIT)).to_list(HISTORY_LIMIT)
    return rows


async def restore(history_id: str, *, expected_version: int, user_id: str, user_name: Optional[str] = None) -> dict[str, Any]:
    """Put back the values a past change replaced. Fields changed since are put back too, and this is itself recorded."""
    row = await _history().find_one({"id": history_id}, {"_id": 0})
    if not row:
        raise SettingsError("That change was not found.")
    existing = await load()
    if int(expected_version) != version():
        raise VersionConflict()
    new_values = deepcopy(existing)
    before_flat = _flatten(existing)
    for change in row.get("changes") or []:
        try:
            _apply(new_values, change["path"], change["before"])
        except (KeyError, TypeError):
            continue
    new_values = _merge(new_values)
    if sum(new_values["trust"][w] for w in WEIGHTS) != 100:
        raise SettingsError("Undoing this would leave the four trust weights not adding up to 100. Change the weights directly instead.")
    after_flat = _flatten(new_values)
    diffs = [{"path": p, "label": describe(p), "before": before_flat[p], "after": after_flat[p]} for p in after_flat if after_flat[p] != before_flat[p]]
    if not diffs:
        raise SettingsError("Those values are already in place.")
    await _write(new_values, version(), _history_row(kind="restore", by=user_id, by_name=user_name, reason="Undid an earlier change", diffs=diffs, ver=version() + 1))
    return current()
