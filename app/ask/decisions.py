"""Deciding which tool a request means, as a swappable step, with a harness to measure candidates and a shadow mode to try one on real traffic safely.

A *decider* is any function that takes the request, the shortlisted tools and the language and says what to do (a tool, or nothing fits). The live one is the small
Groq model in the `decide` role. Others can be added here by name (a small fine-tuned multilingual classifier, a decision model, an embeddings-only chooser) and
are judged on the same labelled requests before anyone switches one on:

* `evaluate` runs a decider over labelled cases and reports accuracy overall and for each language, speed, and every miss;
* shadow mode runs one named candidate beside the live one on each request in a conversation, records what it would have done and whether it agrees, and never
  acts on it (see `runs.py`); Ops reads the agreement and switches it on or off;
* a request is kept 30 days with the run, so no separate copy of what people say is made for training. A test set is a file of labelled requests written for the
  purpose (native speakers for each language), never exported from members' requests without their say-so."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.ask import pipeline, router
from app.db.mongo import get_db
from app.tools.contract import ToolSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    action: str                       # "tool" or "none"
    tool: Optional[str] = None


@dataclass(frozen=True)
class Case:
    text: str
    language: str
    tool: Optional[str]               # the tool a person meant, or None when no tool fits
    reviewed: bool = False            # a native speaker has checked the wording and the label


Decider = Callable[[str, list[ToolSpec], str], Awaitable[Decision]]


# ── deciders ─────────────────────────────────────────────────────────────────

async def live(text: str, candidates: list[ToolSpec], language: str) -> Decision:
    """What the live `decide` role would choose: the small model, a closed choice among the shortlisted tools."""
    prompt, system = pipeline.decide_prompt(text, candidates, language)
    raw = await router.think("decide", prompt, system)
    action, tool, _question, _reason = pipeline.parse_decision(raw, {s.name for s in candidates})
    return Decision("tool", tool) if action == "tool" and tool else Decision("none")


async def shortlist_top(text: str, candidates: list[ToolSpec], language: str) -> Decision:
    """The first shortlisted tool, with no model at all: the shortlist already blends meaning (embeddings) and words. Free and fast, so it is the baseline any model must beat."""
    return Decision("tool", candidates[0].name) if candidates else Decision("none")


#: candidates a name can refer to. A model-backed candidate is added here once it exists; `live` is never a shadow (it is the one being compared against)
CANDIDATES: dict[str, Decider] = {"shortlist_top": shortlist_top}


# ── the harness ──────────────────────────────────────────────────────────────

@dataclass
class Report:
    name: str
    total: int = 0
    correct: int = 0
    by_language: dict[str, dict[str, int]] = field(default_factory=dict)
    misses: list[dict[str, Any]] = field(default_factory=list)
    errors: int = 0
    avg_ms: float = 0.0

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.total, 3) if self.total else 0.0

    def language_accuracy(self) -> dict[str, float]:
        return {lang: round(v["correct"] / v["n"], 3) for lang, v in self.by_language.items() if v["n"]}

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "total": self.total, "accuracy": self.accuracy, "by_language": self.language_accuracy(), "errors": self.errors, "avg_ms": self.avg_ms, "misses": self.misses[:20]}


Shortlister = Callable[[str, list[ToolSpec]], Awaitable[list[ToolSpec]]]


async def evaluate(name: str, decider: Decider, cases: list[Case], specs: list[ToolSpec], shortlister: Shortlister) -> Report:
    """Run `decider` over `cases`. A decider that raises counts as a miss and is counted in `errors`, so a flaky model shows up as flaky."""
    report = Report(name)
    spent = 0.0
    for case in cases:
        candidates = await shortlister(case.text, specs)
        started = time.perf_counter()
        try:
            got = await decider(case.text, candidates, case.language)
        except Exception as exc:  # noqa: BLE001
            logger.info("A decider failed on a case: %s", type(exc).__name__)
            got = Decision("none")
            report.errors += 1
        spent += (time.perf_counter() - started) * 1000
        bucket = report.by_language.setdefault(case.language, {"n": 0, "correct": 0})
        bucket["n"] += 1
        report.total += 1
        right = got.tool == case.tool if case.tool else got.action == "none"
        if right:
            bucket["correct"] += 1
            report.correct += 1
        else:
            report.misses.append({"text": case.text, "language": case.language, "expected": case.tool, "got": got.tool})
    report.avg_ms = round(spent / report.total, 1) if report.total else 0.0
    return report


async def compare(deciders: dict[str, Decider], cases: list[Case], specs: list[ToolSpec], shortlister: Shortlister) -> list[Report]:
    """The same cases through each decider, best first."""
    reports = [await evaluate(name, fn, cases, specs, shortlister) for name, fn in deciders.items()]
    return sorted(reports, key=lambda r: (-r.accuracy, r.avg_ms))


def load_cases(path: Path) -> list[Case]:
    """Labelled requests from a JSON-lines file: {"text", "language", "tool" (or null), "reviewed"}. A line that is not valid is skipped."""
    out: list[Case] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
            out.append(Case(text=str(row["text"]), language=str(row["language"]), tool=row.get("tool") or None, reviewed=bool(row.get("reviewed"))))
        except (ValueError, KeyError, TypeError):
            continue
    return out


# ── shadow mode ──────────────────────────────────────────────────────────────

def config():
    return get_db()["ask_router"]


async def shadow_name() -> Optional[str]:
    """The candidate Ops switched on to run beside the live decision, or None."""
    try:
        doc = await config().find_one({"_id": "shadow"}) or {}
    except Exception:  # noqa: BLE001 - shadow is optional
        return None
    name = doc.get("name")
    return name if isinstance(name, str) and name in CANDIDATES else None


async def set_shadow(name: Optional[str]) -> Optional[str]:
    if name is not None and name not in CANDIDATES:
        raise ValueError("There is no such candidate.")
    await config().update_one({"_id": "shadow"}, {"$set": {"name": name}}, upsert=True)
    return name


async def run_shadow(name: str, text: str, candidates: list[ToolSpec], language: str, live_tool: Optional[str]) -> dict[str, Any]:
    """What the named candidate would have chosen, and whether it agrees with what the live step chose. Never acted on. A failure is recorded, not raised."""
    started = time.perf_counter()
    try:
        got = await CANDIDATES[name](text, candidates, language)
        return {"name": name, "action": got.action, "tool": got.tool, "agrees": got.tool == live_tool, "ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "error": type(exc).__name__, "agrees": False}


async def agreement(days: int = 7) -> list[dict[str, Any]]:
    """For each candidate that ran in shadow lately: how many requests, how often it agreed with the live choice, and how often it errored."""
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = get_db()["ask_runs"].aggregate([
        {"$match": {"created_at": {"$gte": since}, "shadow.name": {"$exists": True}}},
        {"$group": {"_id": "$shadow.name", "n": {"$sum": 1}, "agree": {"$sum": {"$cond": [{"$eq": ["$shadow.agrees", True]}, 1, 0]}},
                    "errors": {"$sum": {"$cond": [{"$ifNull": ["$shadow.error", False]}, 1, 0]}}, "ms": {"$avg": "$shadow.ms"}}},
        {"$sort": {"n": -1}},
    ])
    return [{"name": r["_id"], "requests": r["n"], "agree": r["agree"], "agreement": round(r["agree"] / r["n"], 3) if r["n"] else 0.0, "errors": r["errors"], "avg_ms": round(r.get("ms") or 0.0, 1)} async for r in rows]


from app.ask import candidates as _candidates  # noqa: E402,F401  (adds the free-tier candidates to CANDIDATES; imported last because it uses this module)
