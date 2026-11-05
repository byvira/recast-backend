"""What the quality checks held back, read from the posts themselves.

A post is "held" when its quality check did not pass. Each saved reason is sorted into one named check so Ops can see which check
holds the most and on which platform. Nothing here changes a post.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.mongo import get_db

SCAN_LIMIT = 4000
DISTRIBUTION_LIMIT = 1500
DAY_CHOICES = (7, 30, 90)

# (key, words shown to people), in the order the checks are listed.
CHECKS: list[tuple[str, str]] = [
    ("too_short", "Too short"),
    ("banned_words", "Banned words"),
    ("language", "Wrong language"),
    ("length_limit", "Over the length limit"),
    ("opening_closing", "Generic opening or closing"),
    ("brand_phrase", "Brand phrase missing"),
    ("safety", "Held by the safety check"),
    ("other", "Other"),
]
CHECK_LABEL = dict(CHECKS)

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("too_short", re.compile(r"too short", re.I)),
    ("banned_words", re.compile(r"banned word", re.I)),
    ("language", re.compile(r"not written in|requested language|almost entirely english|mix the two", re.I)),
    ("length_limit", re.compile(r"exceed|character limit|is \d+ characters, the limit", re.I)),
    ("opening_closing", re.compile(r"generic (opening|closing)", re.I)),
    ("brand_phrase", re.compile(r"required brand phrase", re.I)),
    ("safety", re.compile(r"may contain", re.I)),
]
ADVICE_WORDS = re.compile(r"^advisory:", re.I)


def classify(issue: str) -> str:
    """The check a saved reason belongs to."""
    for key, pattern in _RULES:
        if pattern.search(issue or ""):
            return key
    return "other"


def reasons(issues: list[str]) -> list[dict[str, str]]:
    """The reasons that actually held a post: advice-only notes are left out."""
    return [{"check": classify(i), "text": str(i)[:240]} for i in (issues or []) if not ADVICE_WORDS.match(str(i))]


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days if days in DAY_CHOICES else 30)


def _pieces():
    return get_db()["content_pieces"]


async def summary(days: int = 30) -> dict[str, Any]:
    """Counts for the window and the one before it, plus held posts by day, by check and by platform."""
    days = days if days in DAY_CHOICES else 30
    start = _since(days)
    previous_start = start - timedelta(days=days)
    base = {"deleted": {"$ne": True}}
    total = await _pieces().count_documents({**base, "created_at": {"$gte": start}})
    prev_total = await _pieces().count_documents({**base, "created_at": {"$gte": previous_start, "$lt": start}})
    prev_held = await _pieces().count_documents({**base, "created_at": {"$gte": previous_start, "$lt": start}, "quality_passed": False})

    rows = await _pieces().find(
        {**base, "created_at": {"$gte": start}, "quality_passed": False},
        {"_id": 0, "created_at": 1, "platform": 1, "quality_issues": 1},
    ).sort("created_at", -1).limit(SCAN_LIMIT).to_list(SCAN_LIMIT)

    by_check: Counter[str] = Counter()
    by_platform_check: dict[str, Counter[str]] = {}
    by_day: Counter[str] = Counter()
    for row in rows:
        found = reasons(row.get("quality_issues") or [])
        keys = {r["check"] for r in found} or {"other"}
        platform = str(row.get("platform") or "Unknown")
        for key in keys:
            by_check[key] += 1
            by_platform_check.setdefault(platform, Counter())[key] += 1
        created = row.get("created_at")
        if isinstance(created, datetime):
            by_day[created.date().isoformat()] += 1

    held = len(rows)
    today = datetime.now(timezone.utc).date()
    series = [{"day": (today - timedelta(days=days - 1 - i)).isoformat(), "held": by_day.get((today - timedelta(days=days - 1 - i)).isoformat(), 0)} for i in range(days)]
    passed_rate = None if total == 0 else round(100 * (total - held) / total, 1)
    prev_rate = None if prev_total == 0 else round(100 * (prev_total - prev_held) / prev_total, 1)
    return {
        "days": days,
        "posts": total,
        "held": held,
        "capped": held >= SCAN_LIMIT,
        "passed_rate": passed_rate,
        "passed_rate_change": None if passed_rate is None or prev_rate is None else round(passed_rate - prev_rate, 1),
        "by_check": [{"check": k, "label": CHECK_LABEL[k], "count": by_check[k]} for k, _ in CHECKS if by_check[k]],
        "by_platform": [
            {"platform": p, "total": sum(c.values()), "checks": {k: c[k] for k, _ in CHECKS if c[k]}}
            for p, c in sorted(by_platform_check.items(), key=lambda kv: -sum(kv[1].values()))
        ],
        "daily": series,
    }


async def held_list(*, days: int = 30, platform: Optional[str] = None, check: Optional[str] = None, page: int = 0, size: int = 25) -> dict[str, Any]:
    query: dict[str, Any] = {"deleted": {"$ne": True}, "created_at": {"$gte": _since(days)}, "quality_passed": False}
    if platform:
        query["platform"] = platform
    rows = await _pieces().find(
        query, {"_id": 0, "piece_id": 1, "platform": 1, "content": 1, "quality_issues": 1, "readability_score": 1, "created_at": 1,
                "approval_status": 1, "workspace_id": 1},
    ).sort("created_at", -1).limit(SCAN_LIMIT).to_list(SCAN_LIMIT)
    out = []
    for row in rows:
        found = reasons(row.get("quality_issues") or []) or [{"check": "other", "text": "The check did not pass."}]
        if check and check not in {r["check"] for r in found}:
            continue
        out.append({
            "id": row.get("piece_id"), "platform": row.get("platform"), "created_at": row.get("created_at"),
            "excerpt": str(row.get("content") or "")[:200], "reasons": found, "readability": row.get("readability_score"),
            "approval": str(getattr(row.get("approval_status"), "value", row.get("approval_status")) or ""),
        })
    total = len(out)
    return {"total": total, "page": page, "posts": out[page * size:(page + 1) * size]}


async def held_one(piece_id: str) -> Optional[dict[str, Any]]:
    row = await _pieces().find_one(
        {"piece_id": piece_id, "quality_passed": False},
        {"_id": 0, "piece_id": 1, "platform": 1, "content": 1, "quality_issues": 1, "readability_score": 1, "created_at": 1},
    )
    if not row:
        return None
    text = str(row.get("content") or "")
    return {
        "id": row["piece_id"], "platform": row.get("platform"), "created_at": row.get("created_at"), "text": text[:6000],
        "words": len(text.split()), "readability": row.get("readability_score"),
        "reasons": reasons(row.get("quality_issues") or []),
        "advice": [str(i)[:240] for i in (row.get("quality_issues") or []) if ADVICE_WORDS.match(str(i))],
    }


def _histogram(values: list[float], edges: list[float]) -> list[int]:
    counts = [0] * (len(edges) + 1)
    for v in values:
        for i, edge in enumerate(edges):
            if v < edge:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    return counts


READING_EDGES = [10.0 * i for i in range(1, 10)]          # 0-10, 10-20 ... 90-100
WORD_EDGES = [25.0, 50.0, 100.0, 150.0, 200.0, 300.0, 450.0, 600.0, 900.0, 1500.0]


async def distribution(platform: str, days: int = 30) -> dict[str, Any]:
    """How recent posts on one platform spread over reading ease and length, so a change to a bar can be judged by what it would hit."""
    rows = await _pieces().find(
        {"deleted": {"$ne": True}, "platform": platform, "created_at": {"$gte": _since(days)}},
        {"_id": 0, "content": 1, "readability_score": 1},
    ).sort("created_at", -1).limit(DISTRIBUTION_LIMIT).to_list(DISTRIBUTION_LIMIT)
    reading = [float(r["readability_score"]) for r in rows if isinstance(r.get("readability_score"), (int, float))]
    words = [float(len(str(r.get("content") or "").split())) for r in rows]
    return {
        "platform": platform, "posts": len(rows),
        "reading": {"edges": READING_EDGES, "counts": _histogram(reading, READING_EDGES), "measured": len(reading), "values": sorted(round(v) for v in reading)},
        "words": {"edges": WORD_EDGES, "counts": _histogram(words, WORD_EDGES), "values": sorted(int(v) for v in words)},
    }
