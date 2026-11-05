"""How much authority the model's reading has, per kind of problem, earned step by step.

When the free rules find nothing and a model reads a text and says it is unsafe, what happens next depends on the level for that kind of
problem (hate, claims, personal details and so on):

  1 Watch   the finding is only recorded; the post goes on
  2 Advise  the post goes on with a note for the member
  3 Fix     one rewrite is tried, and the post is held if it is still unsafe (this is what has always happened, so it is where every kind starts)
  4 Hold    the post is held straight away

Ops moves a kind up or down, with a reason. The serious kinds (hate, violence, self harm, sexual content) cannot go below Fix. A kind is
"ready" to move up when the model has agreed with the rules often enough and staff rarely release what it held. A kind goes down by itself
when staff release most of what it held. A switch puts everything back to the starting levels. The rules themselves are never affected.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.agents.content_guard.rules import CATEGORIES
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

LEVELS = {1: "Watch", 2: "Advise", 3: "Fix", 4: "Hold"}
START_LEVEL = 3
FLOOR = {"hate": 3, "violence": 3, "self_harm": 3, "sexual": 3}
KINDS = [k for k in CATEGORIES if k != "assistant_leak"]
RELEASED_OK_PERCENT = 20
RELEASED_DEMOTE_PERCENT = 50
MIN_ANSWERED = 5
CACHE_SECONDS = 30
HISTORY_LIMIT = 100

_cache: dict[str, Any] = {"levels": {}, "enabled": True, "version": 0, "loaded_at": 0.0}


class LadderError(ValueError):
    """Something the person can fix; the message is shown as written."""


def _col():
    return get_db()["safety_ladder"]


def _history():
    return get_db()["safety_ladder_history"]


def _decisions():
    return get_db()["safety_decisions"]


def floor_of(kind: str) -> int:
    return FLOOR.get(kind, 1)


def set_cache(levels: dict[str, int], enabled: bool, version: int) -> None:
    _cache.update({"levels": dict(levels), "enabled": bool(enabled), "version": version, "loaded_at": time.monotonic()})


def reset_cache() -> None:
    set_cache({}, True, 0)
    _cache["loaded_at"] = 0.0


async def load() -> dict[str, Any]:
    doc = await _col().find_one({"_id": "config"}) or {}
    set_cache(doc.get("levels") or {}, doc.get("enabled", True), int(doc.get("version", 0)))
    return _cache


async def ensure_fresh() -> None:
    if time.monotonic() - float(_cache["loaded_at"]) > CACHE_SECONDS:
        try:
            await load()
        except Exception:  # noqa: BLE001 - the last good levels stay
            _cache["loaded_at"] = time.monotonic()


def current_level(kind: str) -> int:
    """The level for one kind of problem right now. With the switch off, or for a kind never set, it is the starting level."""
    if not _cache["enabled"]:
        return max(START_LEVEL, floor_of(kind))
    return max(int(_cache["levels"].get(kind, START_LEVEL)), floor_of(kind))


async def level_for(categories: list[str]) -> int:
    """The level that applies to a finding: the strictest among its kinds, so a finding that includes a serious kind is held as that kind
    is, whatever the level of the other kinds in it."""
    await ensure_fresh()
    return max((current_level(c) for c in categories), default=START_LEVEL)


async def log_decision(*, categories: list[str], level: int, action: str, language: str, platform: Optional[str], text: str, stage: str) -> None:
    """One row for what the reading said and what was done about it, with the text hidden and short. Never raises."""
    try:
        from app.agents.content_guard.sensitive import mask_personal

        await _decisions().insert_one({
            "id": str(uuid4()), "at": datetime.now(timezone.utc), "categories": categories, "level": level, "action": action,
            "language": language or "unknown", "platform": platform, "stage": stage, "excerpt": mask_personal(text)[:160],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not keep a safety decision: %s", exc)


# ---- readiness ------------------------------------------------------------------------------------------------------------------------
async def readiness(days: int = 30) -> dict[str, dict[str, Any]]:
    """For each kind: what the model said, how often staff released what it held, and whether it has earned a step up."""
    from app.agents.content_guard import judges
    from app.quality import settings as QS

    await QS.load()
    trust = QS.trust()
    need = int(trust["recommend_min_decisions"])
    agree_need = float(trust["recommend_min_agreement"])
    comparison = await judges.comparison_summary(days)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: dict[str, dict[str, Any]] = {}
    for kind in KINDS:
        answered = released = 0
        async for a in get_db()["safety_appeals"].find({"created_at": {"$gte": since}, "source": "model", "categories": kind, "status": {"$in": ["released", "upheld"]}}, {"status": 1}):
            answered += 1
            released += 1 if a["status"] == "released" else 0
        acted = await _decisions().count_documents({"at": {"$gte": since}, "categories": kind})
        share = None if answered == 0 else round(100 * released / answered, 1)
        reasons: list[str] = []
        if comparison["compared"] < need:
            reasons.append(f"Needs {need} compared posts, has {comparison['compared']}")
        elif (comparison["agreement"] or 0) < agree_need:
            reasons.append(f"The two models agree {comparison['agreement']}%, {agree_need:g}% needed")
        if answered >= MIN_ANSWERED and (share or 0) > RELEASED_OK_PERCENT:
            reasons.append(f"Staff released {share}% of {answered} answered reviews, {RELEASED_OK_PERCENT}% or less needed")
        out[kind] = {"kind": kind, "label": CATEGORIES.get(kind, kind), "answered": answered, "released": released, "released_share": share, "decisions": acted,
                     "ready": not reasons, "reasons": reasons}
    return out


async def snapshot() -> dict[str, Any]:
    await load()
    ready = await readiness()
    return {
        "enabled": bool(_cache["enabled"]), "version": int(_cache["version"]), "start_level": START_LEVEL, "levels": {str(k): name for k, name in LEVELS.items()},
        "kinds": [{"kind": k, "label": CATEGORIES.get(k, k), "level": current_level(k), "floor": floor_of(k), **{x: ready[k][x] for x in ("answered", "released", "released_share", "decisions", "ready", "reasons")}}
                  for k in KINDS],
    }


# ---- changing it ----------------------------------------------------------------------------------------------------------------------
def _reason(text: Any) -> str:
    out = " ".join(str(text or "").split())[:200]
    if len(out) < 4:
        raise LadderError("Say in a few words why.")
    return out


async def save(*, levels: dict[str, Any], enabled: Optional[bool], reason: Any, expected_version: int, user_id: str, user_name: Optional[str], automatic: bool = False) -> dict[str, Any]:
    """Move kinds to new levels and/or switch the ladder on or off. Needs a reason, respects the floor, and is kept in the history."""
    await load()
    if int(expected_version) != int(_cache["version"]):
        raise LadderError("The levels changed while you were looking. Reload and try again.")
    why = _reason(reason)
    new = dict(_cache["levels"])
    changes = []
    for kind, value in (levels or {}).items():
        if kind not in KINDS:
            raise LadderError(f"“{str(kind)[:30]}” is not a kind of problem.")
        if isinstance(value, bool) or not isinstance(value, int) or value not in LEVELS:
            raise LadderError("A level is Watch, Advise, Fix or Hold.")
        if value < floor_of(kind):
            raise LadderError(f"{CATEGORIES.get(kind, kind)} cannot go below {LEVELS[floor_of(kind)]}.")
        before = current_level(kind)
        if value != before:
            changes.append({"path": kind, "label": CATEGORIES.get(kind, kind), "before": LEVELS[before], "after": LEVELS[value]})
            new[kind] = value
    switch = bool(_cache["enabled"]) if enabled is None else bool(enabled)
    if switch != bool(_cache["enabled"]):
        changes.append({"path": "enabled", "label": "Use the levels", "before": "on" if _cache["enabled"] else "off", "after": "on" if switch else "off"})
    if not changes:
        raise LadderError("Nothing was changed.")
    ver = int(_cache["version"]) + 1
    await _col().update_one({"_id": "config"}, {"$set": {"levels": new, "enabled": switch, "version": ver, "updated_at": datetime.now(timezone.utc), "updated_by": user_id}}, upsert=True)
    set_cache(new, switch, ver)
    await _history().insert_one({"id": str(uuid4()), "at": datetime.now(timezone.utc), "by": user_id, "by_name": user_name, "automatic": automatic, "reason": why, "changes": changes, "version": ver})
    return await snapshot()


async def history(limit: int = 50) -> list[dict[str, Any]]:
    return await _history().find({}, {"_id": 0}).sort("at", -1).limit(min(max(limit, 1), HISTORY_LIMIT)).to_list(HISTORY_LIMIT)


async def demote_where_released(days: int = 30) -> list[str]:
    """A kind that staff release most of the time loses a step by itself, so a check that keeps being wrong stops having its way. Returns
    the kinds moved down. Never goes below the floor or below Watch."""
    await load()
    if not _cache["enabled"]:
        return []
    moved: list[str] = []
    ready = await readiness(days)
    for kind, r in ready.items():
        level = current_level(kind)
        if r["answered"] >= MIN_ANSWERED and (r["released_share"] or 0) > RELEASED_DEMOTE_PERCENT and level - 1 >= floor_of(kind) and level > 1:
            try:
                await save(levels={kind: level - 1}, enabled=None, reason=f"Staff released {r['released_share']}% of {r['answered']} reviews", expected_version=int(_cache["version"]),
                           user_id="system", user_name="Automatic", automatic=True)
                moved.append(kind)
            except LadderError:
                continue
    return moved
