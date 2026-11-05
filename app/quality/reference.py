"""A saved set of example drafts that an analyst checks every change against.

Each example is a draft written for one platform and language, with what should happen to it: `pass` (it is good and should go through
the writing checks) or `held` (it is bad and should be held). After changing a number or a word list, running the set shows at once
which examples now behave differently, using today's saved values or the changes not saved yet. Only the free checks run: no model is
called and nothing is charged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db
from app.quality import defaults as D
from app.quality import settings as QS

TOTAL_MAX = 100
PER_PLATFORM_MAX = 25
TEXT_MAX = 6000
NOTE_MAX = 200
EXPECT = ("pass", "held")


class ReferenceError(ValueError):
    """Something the person can fix; the message is shown as written."""


def col():
    return get_db()["quality_reference"]


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "_id"}


async def add(*, platform: str, language: str, text: str, expect: str, note: Any, user_id: str) -> dict[str, Any]:
    if platform not in D.PLATFORMS:
        raise ReferenceError("That is not a platform.")
    if expect not in EXPECT:
        raise ReferenceError("Say whether the draft should pass or be held.")
    body = (text or "").strip()
    if len(body) < 10:
        raise ReferenceError("Paste the draft to save.")
    if len(body) > TEXT_MAX:
        raise ReferenceError(f"A draft can be up to {TEXT_MAX} characters.")
    if await col().count_documents({}) >= TOTAL_MAX:
        raise ReferenceError(f"The set holds up to {TOTAL_MAX} drafts. Remove one first.")
    if await col().count_documents({"platform": platform}) >= PER_PLATFORM_MAX:
        raise ReferenceError(f"{platform} can hold up to {PER_PLATFORM_MAX} drafts. Remove one first.")
    row = {"id": str(uuid4()), "platform": platform, "language": (language or "en").strip().lower()[:20] or "en", "text": body, "expect": expect,
           "note": " ".join(str(note or "").split())[:NOTE_MAX], "added_by": user_id, "created_at": datetime.now(timezone.utc)}
    await col().insert_one(dict(row))
    return _public(row)


async def remove(example_id: str) -> bool:
    return (await col().delete_one({"id": example_id})).deleted_count > 0


async def listing() -> list[dict[str, Any]]:
    rows = await col().find({}, {"_id": 0}).sort([("platform", 1), ("created_at", 1)]).to_list(TOTAL_MAX)
    return rows


async def run(changes: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Every example through the writing checks and the free safety rules. With `changes`, the values are the saved ones with those laid
    over them, tried without saving. Each result says whether it behaved as expected."""
    from copy import deepcopy

    from app.agents.content_guard.agent import rule_screen
    from app.models.text import Platform
    from app.pipelines.text.quality import run_quality_gate

    live = await QS.load()
    values = live
    if changes:
        tidy = QS.clean_changes(changes, live)
        values = deepcopy(live)
        for group in ("min_words", "readability", "trust", "patterns"):
            values[group].update(tidy.get(group) or {})
        for name in QS.LISTS:
            if name in tidy:
                values[name] = tidy[name]
    results = []
    with QS.trying(values):
        for ex in await listing():
            gate = await run_quality_gate(ex["text"], Platform(ex["platform"]), brand_context="", banned_words=[], avoid_blacklist=False, language=ex["language"])
            safety = rule_screen(ex["text"], language=ex["language"])
            reasons = [i for i in gate.issues if not i.lower().startswith("advisory:")] + ([safety.message()] if not safety.ok else [])
            outcome = "pass" if gate.passed and safety.ok else "held"
            results.append({"id": ex["id"], "platform": ex["platform"], "language": ex["language"], "expect": ex["expect"], "outcome": outcome,
                            "as_expected": outcome == ex["expect"], "reasons": reasons[:4], "excerpt": ex["text"][:140], "note": ex["note"]})
    off = [r for r in results if not r["as_expected"]]
    return {"using": "your unsaved values" if changes else "the saved values", "total": len(results), "as_expected": len(results) - len(off), "different": len(off),
            "results": sorted(results, key=lambda r: (r["as_expected"], r["platform"]))}
