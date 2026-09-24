"""Engagement patterns — what separates a member's (or workspace's) better
posts from the rest, measured on real post-publish numbers.

Input: ``post_metric_checkpoints`` rows at one fixed age (24h), joined to the
piece's own content. Every comparison is winner-group vs. everything else,
by median engagement rate, and only reported when:

* each side has at least ``MIN_PER_SIDE`` posts, and
* the winner's median is at least ``MIN_RATIO`` × the rest's (and the rest's
  median is above zero — a multiple of zero isn't a finding).

No LLM involved: the finding is arithmetic, the wording is a template.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db.mongo import content_pieces, post_metric_checkpoints

CHECKPOINT = "24h"
LOOKBACK = timedelta(days=90)
MAX_SAMPLES = 300
MIN_PER_SIDE = 3
MIN_RATIO = 1.5
SHORT_WORDS = 80
#: Four-hour posting windows (local hours).
TIME_WINDOWS = [(6, 10), (10, 14), (14, 18), (18, 22)]


@dataclass(frozen=True)
class Finding:
    variant: str           # template suffix: platform / length_short / … / time
    ratio: float
    n: int                 # posts compared
    params: dict           # template placeholders (winner, words, start, end)

    @property
    def key(self) -> str:
        """Stable identity for dedupe + feedback suppression — the variant
        plus whatever makes it specific (which platform, which window)."""
        extra = self.params.get("winner") or (
            f"{self.params['start']}-{self.params['end']}" if "start" in self.params else ""
        )
        return f"{self.variant}:{extra}" if extra else self.variant


async def load_samples(workspace_id: str, user_id: Optional[str] = None) -> list[dict]:
    query: dict = {
        "workspace_id": workspace_id,
        "checkpoint": CHECKPOINT,
        "captured_at": {"$gte": datetime.now(timezone.utc) - LOOKBACK},
    }
    if user_id:
        query["user_id"] = user_id
    rows = await post_metric_checkpoints.find(
        query, {"piece_id": 1, "platform": 1, "word_count": 1, "published_at": 1, "metrics.engagement_rate": 1},
    ).sort("captured_at", -1).limit(MAX_SAMPLES).to_list(length=MAX_SAMPLES)
    if not rows:
        return []

    openers: dict[str, str] = {}
    async for piece in content_pieces.find(
        {"piece_id": {"$in": [r["piece_id"] for r in rows]}}, {"piece_id": 1, "content": 1},
    ):
        first_line = (piece.get("content") or "").strip().splitlines()
        openers[piece["piece_id"]] = first_line[0] if first_line else ""

    return [
        {
            "platform": r.get("platform") or "",
            "engagement": float((r.get("metrics") or {}).get("engagement_rate") or 0.0),
            "words": int(r.get("word_count") or 0),
            "question_opener": openers.get(r["piece_id"], "").rstrip().endswith("?"),
            "published_at": r.get("published_at"),
        }
        for r in rows
    ]


def _compare(winners: list[float], rest: list[float]) -> Optional[float]:
    if len(winners) < MIN_PER_SIDE or len(rest) < MIN_PER_SIDE:
        return None
    w, r = statistics.median(winners), statistics.median(rest)
    if r <= 0:
        return None
    ratio = w / r
    return round(ratio, 1) if ratio >= MIN_RATIO else None


def _local_hour(published_at, tz: ZoneInfo) -> Optional[int]:
    if not isinstance(published_at, datetime):
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(tz).hour


def find_patterns(samples: list[dict], tz_name: str = "UTC") -> list[Finding]:
    """Every qualifying finding, strongest first."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    findings: list[Finding] = []
    n = len(samples)

    # Platform: best platform vs. all others.
    by_platform: dict[str, list[float]] = {}
    for s in samples:
        by_platform.setdefault(s["platform"], []).append(s["engagement"])
    if len(by_platform) >= 2:
        best = max(by_platform, key=lambda p: statistics.median(by_platform[p]))
        rest = [e for p, es in by_platform.items() if p != best for e in es]
        ratio = _compare(by_platform[best], rest)
        if ratio:
            findings.append(Finding("platform", ratio, n, {"winner": best.capitalize()}))

    # Length: short vs. long, whichever way it goes.
    short = [s["engagement"] for s in samples if 0 < s["words"] < SHORT_WORDS]
    long_ = [s["engagement"] for s in samples if s["words"] >= SHORT_WORDS]
    for winners, rest, variant in ((short, long_, "length_short"), (long_, short, "length_long")):
        ratio = _compare(winners, rest)
        if ratio:
            findings.append(Finding(variant, ratio, len(short) + len(long_), {"words": SHORT_WORDS}))

    # Opener: question vs. statement.
    q = [s["engagement"] for s in samples if s["question_opener"]]
    st = [s["engagement"] for s in samples if not s["question_opener"]]
    for winners, rest, variant in ((q, st, "opener_question"), (st, q, "opener_statement")):
        ratio = _compare(winners, rest)
        if ratio:
            findings.append(Finding(variant, ratio, n, {}))

    # Time of day: best four-hour window vs. everything else.
    windowed = [(s, _local_hour(s["published_at"], tz)) for s in samples]
    best_window: Optional[tuple[int, int]] = None
    best_ratio: Optional[float] = None
    for start, end in TIME_WINDOWS:
        inside = [s["engagement"] for s, h in windowed if h is not None and start <= h < end]
        outside = [s["engagement"] for s, h in windowed if h is not None and not (start <= h < end)]
        ratio = _compare(inside, outside)
        if ratio and (best_ratio is None or ratio > best_ratio):
            best_window, best_ratio = (start, end), ratio
    if best_window and best_ratio:
        findings.append(Finding("time", best_ratio, n, {"start": best_window[0], "end": best_window[1]}))

    findings.sort(key=lambda f: f.ratio, reverse=True)
    return findings
