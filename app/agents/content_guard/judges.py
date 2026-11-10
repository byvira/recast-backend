"""Which models read a text for the safety check, in what order, and a silent second opinion.

The read goes down a chain Ops sets (for example the safety model first, then the everyday model). A model that fails three times in
a row is skipped for five minutes so a bad hour does not slow every post. When no model answers, the free rules are the only screen,
as before. A second model can also read a share of posts in the background and only be compared, never obeyed: that is how it earns
trust before it is given any authority.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional

from app.shared.llm import GroqModel

logger = logging.getLogger(__name__)

# The names Ops sees, and the model each one is.
JUDGES: dict[str, GroqModel] = {
    "fast": GroqModel.FAST,
    "balanced": GroqModel.BALANCED,
    "safeguard": GroqModel.SAFEGUARD,
}
JUDGE_LABELS = {
    "fast": "Everyday model (fast)",
    "balanced": "Larger model",
    "safeguard": "Safety model (reads our own policy)",
}
DEFAULT_CHAIN = ["fast"]
MAX_FAILURES = 3
SKIP_SECONDS = 300
LOG_EXCERPT = 200

_state: dict[str, list[float]] = {}  # name -> [consecutive failures, skip until (monotonic)]


def chain(cfg: dict[str, Any]) -> list[str]:
    """The judges to try, in order, from Ops' list; unknown names are dropped and the everyday model is always the last resort."""
    names = [n for n in (cfg.get("judge_models") or DEFAULT_CHAIN) if n in JUDGES]
    out: list[str] = []
    for n in names:
        if n not in out:
            out.append(n)
    if "fast" not in out:
        out.append("fast")
    return out


def usable(name: str) -> bool:
    failures, until = _state.get(name, [0, 0.0])
    return failures < MAX_FAILURES or time.monotonic() >= until


def note_ok(name: str) -> None:
    _state[name] = [0, 0.0]


def note_failure(name: str) -> None:
    failures = _state.get(name, [0, 0.0])[0] + 1
    _state[name] = [failures, time.monotonic() + SKIP_SECONDS if failures >= MAX_FAILURES else 0.0]


def reset() -> None:
    _state.clear()


def should_shadow(cfg: dict[str, Any]) -> Optional[str]:
    """The judge to run silently on this post, or None. Never the judge that already answered, never one that is tripped."""
    name = cfg.get("shadow_model")
    if name not in JUDGES or not usable(name):
        return None
    rate = float(cfg.get("shadow_rate") or 0)
    return name if rate > 0 and random.random() < min(rate, 1.0) else None


def log_col():
    from app.db.mongo import get_db

    return get_db()["safety_judge_log"]


async def record_comparison(*, primary: str, primary_ok: bool, primary_categories: list[str], shadow: str, shadow_ok: Optional[bool],
                            shadow_categories: list[str], text: str, language: str, error: Optional[str] = None) -> None:
    """One row per silent comparison. The text is stored masked and short, so staff see enough to judge a disagreement."""
    from app.agents.content_guard.sensitive import mask_personal

    try:
        await log_col().insert_one({
            "at": datetime.now(timezone.utc), "primary": primary, "primary_ok": primary_ok, "primary_categories": primary_categories,
            "shadow": shadow, "shadow_ok": shadow_ok, "shadow_categories": shadow_categories, "language": language or "unknown",
            "agree": None if shadow_ok is None else (shadow_ok == primary_ok), "error": error, "excerpt": mask_personal(text)[:LOG_EXCERPT],
        })
    except Exception as exc:  # noqa: BLE001 - a log problem must never touch a post
        logger.warning("Could not save a judge comparison: %s", exc)


def spawn(coro) -> None:
    """Run a background comparison without making the post wait. Held so it is not collected before it finishes."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


_tasks: set[asyncio.Future] = set()


def misses_col():
    from app.db.mongo import get_db

    return get_db()["safety_model_misses"]


async def note_unavailable(language: Optional[str]) -> None:
    """Every judge failed for one check, so the post was screened by the rules alone. Counted per day and language so Ops can see how often
    that happens; saving the count never blocks or fails the check."""
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await misses_col().update_one(
            {"_id": day}, {"$inc": {"count": 1, f"by_language.{(language or 'unknown').replace('.', '_')}": 1}, "$setOnInsert": {"at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001 - a missing count must not touch the check
        logger.warning("Could not count a safety model miss: %s", exc)


async def comparison_summary(days: int = 30) -> dict[str, Any]:
    """Agreement between the answering judge and the silent one, by language, and the latest disagreements."""
    from datetime import timedelta

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await log_col().find({"at": {"$gte": since}}, {"_id": 0}).sort("at", -1).limit(2000).to_list(2000)
    by_language: dict[str, dict[str, int]] = {}
    for r in rows:
        bucket = by_language.setdefault(r.get("language") or "unknown", {"compared": 0, "agree": 0, "errors": 0})
        if r.get("agree") is None:
            bucket["errors"] += 1
            continue
        bucket["compared"] += 1
        bucket["agree"] += 1 if r["agree"] else 0
    compared = sum(b["compared"] for b in by_language.values())
    agreed = sum(b["agree"] for b in by_language.values())
    return {
        "days": days, "compared": compared, "agreement": None if compared == 0 else round(100 * agreed / compared, 1),
        "errors": sum(b["errors"] for b in by_language.values()),
        "by_language": [{"language": k, **v, "agreement": None if v["compared"] == 0 else round(100 * v["agree"] / v["compared"], 1)}
                        for k, v in sorted(by_language.items(), key=lambda kv: -kv[1]["compared"])],
        "disagreements": [r for r in rows if r.get("agree") is False][:20],
        "model_unavailable": await _misses(since),
    }


async def _misses(since: datetime) -> dict[str, Any]:
    """How many checks ran on the rules alone because no judge answered, in total and by day."""
    rows = await misses_col().find({"at": {"$gte": since}}).sort("_id", -1).limit(120).to_list(120)
    return {"total": sum(int(r.get("count") or 0) for r in rows), "by_day": [{"day": r["_id"], "count": int(r.get("count") or 0)} for r in rows]}
