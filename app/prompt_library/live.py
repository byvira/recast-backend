"""What Ops has published, held in memory so a prompt is built without waiting on the database.

The prompt loader runs inside plain (non-async) functions on every model call, so it never reads the database itself. A
background task loads every live version, the switches and the live Ops data lists here at start-up and then every few
seconds, and swaps them in all at once. Until the first load, or if the database cannot be read, nothing is applied and
every prompt is exactly its file.

A published version applies to a prompt only when the master switch is on, the prompt's group is switched on, and the
prompt is not a Dev prompt. Only editable sections that still exist in the file are replaced; locked text is never stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.prompt_library.regions import compose, parse_regions

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 15
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Prompts switch to their published versions group by group, lowest risk first.
GROUPS: list[tuple[str, str]] = [
    ("quick", "Quick actions, calls to action and hashtags"),
    ("lists", "Ops data lists (tones, goals, language notes, quality words)"),
    ("platform", "Platform rules and refining"),
    ("workspace", "Brand, campaigns, analytics, suggestions, search and support"),
    ("media", "Pictures, audio and video"),
    ("core", "Safety checks and the main post writer"),
]
GROUP_IDS = [g for g, _ in GROUPS]

_CORE = {"text/generate/master", "text/repurpose/master", "text/refine/system", "text/refine/system_prefix"}


def group_of(path: str) -> str:
    """The switch group a prompt belongs to."""
    if path in _CORE or path.startswith("content_guard/") or path.startswith("fragments/"):
        return "core"
    if path.startswith(("text/refine/chips/", "text/generate/cta_rules/", "text/generate/hashtag_rules/")):
        return "quick"
    if path.startswith(("text/generate/platform_rules/", "text/refine/")):
        return "platform"
    if path.startswith(("media/", "audio/", "video/")):
        return "media"
    if path.startswith("text/"):
        return "core"
    return "workspace"


def _load_manifest() -> dict[str, dict]:
    try:
        return json.loads((PROMPTS_DIR / "manifest.json").read_text(encoding="utf-8"))["prompts"]
    except (OSError, ValueError, KeyError):
        logger.error("The prompt list (app/prompts/manifest.json) could not be read; published versions are not applied.")
        return {}


@dataclass
class Snapshot:
    """Everything published, as of one refresh."""

    enabled: bool = False
    groups: frozenset[str] = frozenset()
    # path -> {"number": int, "texts": {section id: text}}
    versions: dict[str, dict] = field(default_factory=dict)
    # kind -> {"number": int, "data": list}
    ops_data: dict[str, dict] = field(default_factory=dict)
    loaded_at: float = 0.0
    counter: int = 0


_snapshot = Snapshot()
_manifest: dict[str, dict] | None = None
_fallbacks: dict[str, dict[str, Any]] = {}
_fallback_lock = threading.Lock()


def manifest() -> dict[str, dict]:
    global _manifest
    if _manifest is None:
        _manifest = _load_manifest()
    return _manifest


def snapshot() -> Snapshot:
    return _snapshot


def set_snapshot(snap: Snapshot) -> None:
    """Swap in a new snapshot (the refresher, and tests)."""
    global _snapshot
    _snapshot = snap


def applies_to(path: str, snap: Snapshot | None = None) -> bool:
    """Whether a published version may be used for this prompt right now."""
    snap = snap or _snapshot
    if not snap.enabled or path not in snap.versions:
        return False
    info = manifest().get(path)
    if not info or info.get("klass") == "dev":
        return False
    return group_of(path) in snap.groups


_editable_cache: dict[str, tuple[float, frozenset[str]]] = {}
_failed: dict[str, str] = {}


def _editable_ids(path: str) -> frozenset[str]:
    """The editable section ids the prompt file has now (read again when the file changes)."""
    import os

    file = PROMPTS_DIR / f"{path}.jinja"
    try:
        mtime = os.path.getmtime(file)
    except OSError:
        return frozenset()
    cached = _editable_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    text = file.read_text(encoding="utf-8").replace("\r\n", "\n")
    ids = frozenset(r.id for r in parse_regions(text) if r.kind == "editable")
    _editable_cache[path] = (mtime, ids)
    return ids


def live_key(path: str) -> str | None:
    """A label for what is applied to a prompt now, or None when it is the plain file: no version applies, none of its
    sections still exist, or this version already failed (so it is not rebuilt and retried on every call)."""
    if not applies_to(path):
        return None
    version = _snapshot.versions[path]
    key = f"v{version['number']}"
    if _failed.get(path) == _identity(version) or not (set(version["texts"]) & _editable_ids(path)):
        return None
    return key


def _identity(version: dict) -> str:
    """The exact published text of a version, so a remembered failure never matches a different version that happens to
    reuse its number."""
    import hashlib

    body = "\x00".join(f"{k}\x01{v}" for k, v in sorted(version["texts"].items()))
    return f"v{version['number']}:{hashlib.sha1(body.encode('utf-8')).hexdigest()[:12]}"


def mark_failed(path: str, key: str | None) -> None:
    version = _snapshot.versions.get(path)
    if key and version:
        _failed[path] = _identity(version)


def forget_failures() -> None:
    _failed.clear()


def compose_live(path: str, source: str) -> tuple[str, str] | None:
    """The file with the published text put into its editable sections, and the label of that version. None when no
    version applies or none of its sections still exist (the plain file is used)."""
    snap = _snapshot
    if not applies_to(path, snap):
        return None
    version = snap.versions[path]
    regions = parse_regions(source.replace("\r\n", "\n"))
    editable = {r.id for r in regions if r.kind == "editable"}
    texts = {rid: text for rid, text in version["texts"].items() if rid in editable}
    if not texts:
        return None
    return compose(regions, texts), f"v{version['number']}"


def ops_data(kind: str) -> list | None:
    """The live Ops list of a kind, or None when the code values are used. Applies only while the lists group is on."""
    snap = _snapshot
    if not snap.enabled or "lists" not in snap.groups:
        return None
    entry = snap.ops_data.get(kind)
    return entry["data"] if entry else None


def record_fallback(path: str, error: BaseException) -> None:
    """A published version could not be used and the file was used instead. Counted for Needs attention."""
    with _fallback_lock:
        item = _fallbacks.setdefault(path, {"count": 0, "last_error": "", "last_at": 0.0, "version": None})
        item["count"] += 1
        item["last_error"] = f"{type(error).__name__}: {str(error)[:300]}"
        item["last_at"] = time.time()
        item["version"] = live_key(path)
    mark_failed(path, item["version"])
    logger.warning("Published version of %s could not be used; the code version was used instead (%s).", path, error)


def fallbacks() -> dict[str, dict[str, Any]]:
    with _fallback_lock:
        return {k: dict(v) for k, v in _fallbacks.items()}


def clear_fallbacks(path: str | None = None) -> None:
    with _fallback_lock:
        if path is None:
            _fallbacks.clear()
        else:
            _fallbacks.pop(path, None)


# ---------------------------------------------------------------------------------------------------------------------
# Loading from the database
# ---------------------------------------------------------------------------------------------------------------------

async def load(db) -> Snapshot:
    """Reads the switches, every live prompt version and every live Ops list."""
    settings_doc = await db.prompt_library_settings.find_one({"_id": "switches"}) or {}
    versions: dict[str, dict] = {}
    async for d in db.prompt_state.find({"versions.status": "live"}, {"_id": 1, "versions.$": 1}):
        v = (d.get("versions") or [None])[0]
        if v:
            versions[d["_id"]] = {"number": v["number"], "texts": dict(v.get("regionTexts") or {})}
    lists: dict[str, dict] = {}
    async for d in db.prompt_ops_data.find({"versions.status": "live"}, {"_id": 1, "versions.$": 1}):
        v = (d.get("versions") or [None])[0]
        if v:
            lists[d["_id"]] = {"number": v["number"], "data": v.get("data")}
    return Snapshot(
        enabled=bool(settings_doc.get("enabled")),
        groups=frozenset(g for g in settings_doc.get("groups") or [] if g in GROUP_IDS),
        versions=versions,
        ops_data=lists,
        loaded_at=time.time(),
        counter=int(settings_doc.get("counter") or 0),
    )


async def refresh(db) -> bool:
    try:
        set_snapshot(await load(db))
        return True
    except Exception as exc:  # noqa: BLE001 - a failed read keeps the last good snapshot
        logger.warning("Could not refresh the published prompt versions: %s", exc)
        return False


async def flush_fallbacks(db) -> None:
    """Saves the fallbacks counted on this server, so Needs attention shows them whichever server answers."""
    pending = fallbacks()
    if not pending:
        return
    clear_fallbacks()
    for path, item in pending.items():
        try:  # a count that can't be saved is put back for the next try
            await db.prompt_fallbacks.update_one(
                {"_id": path},
                {"$inc": {"count": item["count"]},
                 "$set": {"lastAt": item["last_at"], "lastError": item["last_error"], "version": item["version"]}},
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save a prompt fallback for %s: %s", path, exc)
            with _fallback_lock:
                again = _fallbacks.setdefault(path, {"count": 0, "last_error": "", "last_at": 0.0, "version": None})
                again["count"] += item["count"]
                again["last_error"] = again["last_error"] or item["last_error"]
                again["last_at"] = max(again["last_at"], item["last_at"])
                again["version"] = again["version"] or item["version"]


_task: asyncio.Task | None = None


async def _loop() -> None:
    from app.db.mongo import get_db

    while True:
        try:
            db = get_db()
            await refresh(db)
            await flush_fallbacks(db)
        except Exception as exc:  # noqa: BLE001 - the loop must keep running; the last good snapshot stays
            logger.warning("Prompt library refresh failed: %s", exc)
        await asyncio.sleep(REFRESH_SECONDS)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="prompt-library-refresh")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
