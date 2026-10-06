"""Suggested times to publish a post.

With enough of the workspace's own results on a platform, the suggestions are the times of day and kinds of day (weekday or weekend)
where its posts did best, measured by engagement rate one day after posting. With too few results, they are common times for the
platform and are labelled as general patterns, never as the member's own. A suggestion is never a promise of results.

Pure functions only: `rank_slots` takes the results and the already planned times, so it can be tested without a database.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Fewest measured posts on a platform before its own history is used.
MIN_HISTORY = 10
#: The earliest a suggestion can be, so the member has time to look and change it.
LEAD = timedelta(minutes=30)
#: Another post on the same platform this close to a suggestion counts as a clash.
CLASH_WINDOW = timedelta(hours=2)
MAX_SLOTS = 3

#: Local windows (start hour, end hour), and how they read in a sentence.
WINDOWS = [
    (6, 10, "mornings"),
    (10, 14, "around lunchtime"),
    (14, 18, "afternoons"),
    (18, 22, "evenings"),
]

#: Common times for each platform when there are too few results, as (weekday type, local hour). Widely repeated rules of thumb,
#: not measurements, which is why they are labelled "general pattern".
GENERAL_PATTERNS: dict[str, list[tuple[str, int]]] = {
    "linkedin": [("weekday", 9), ("weekday", 12), ("weekday", 8)],
    "facebook": [("weekday", 10), ("weekday", 13), ("weekend", 10)],
    "instagram": [("weekday", 12), ("weekday", 19), ("weekend", 10)],
    "threads": [("weekday", 9), ("weekday", 18), ("weekend", 11)],
    "bluesky": [("weekday", 10), ("weekday", 15), ("weekend", 11)],
    "youtube": [("weekday", 15), ("weekend", 10), ("weekday", 17)],
}
DEFAULT_PATTERN = [("weekday", 10), ("weekday", 13), ("weekend", 10)]


@dataclass
class Slot:
    at: datetime  # UTC
    reason: str
    confidence: str  # high | medium | low | general
    clash: Optional[str] = None

    def to_dict(self) -> dict:
        return {"at": self.at.isoformat(), "reason": self.reason, "confidence": self.confidence, "clash": self.clash}


def get_zone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _as_utc(value) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _daytype(local: datetime) -> str:
    return "weekend" if local.weekday() >= 5 else "weekday"


def _window_index(hour: int) -> Optional[int]:
    for i, (start, end, _) in enumerate(WINDOWS):
        if start <= hour < end:
            return i
    return None


def _phrase(daytype: str, window: int) -> str:
    return f"on {daytype} {WINDOWS[window][2]}" if window != 1 else f"on {daytype}s around lunchtime"


def _next_occurrence(now: datetime, zone: ZoneInfo, daytype: str, hour: int, after: datetime) -> Optional[datetime]:
    local_now = after.astimezone(zone)
    for offset in range(0, 15):
        day = (local_now + timedelta(days=offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if _daytype(day) != daytype:
            continue
        if day.astimezone(timezone.utc) >= after:
            return day.astimezone(timezone.utc)
    return None


def _clash_note(candidate: datetime, planned: list[datetime], label: str, zone: ZoneInfo) -> Optional[str]:
    for other in planned:
        if abs(other - candidate) < CLASH_WINDOW:
            local = other.astimezone(zone)
            when = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
            return f"Another {label} post is already planned for {when} that day. Posts close together can compete. Keep this time or choose another."
    return None


def _confidence(n: int, ratio: float) -> str:
    if n >= 8 and ratio >= 1.3:
        return "high"
    if n >= 4 and ratio >= 1.1:
        return "medium"
    return "low"


def rank_slots(
    *, platform: str, label: str, samples: list[dict], planned: list[datetime], tz_name: Optional[str], now: Optional[datetime] = None,
) -> dict:
    """The suggested slots for one platform.

    `samples` are {"published_at", "engagement"} rows for this platform; `planned` the UTC times of posts already queued for it.
    Returns {"basis": "history" | "general", "slots": [...], "note": str | None, "timezone": str}.
    """
    now = now or datetime.now(timezone.utc)
    zone = get_zone(tz_name)
    after = now + LEAD
    planned = sorted({p for p in (_as_utc(p) for p in planned) if p})

    measured: list[tuple[str, int, int, float]] = []  # daytype, window, hour, engagement
    for row in samples:
        moment = _as_utc(row.get("published_at"))
        if moment is None:
            continue
        local = moment.astimezone(zone)
        window = _window_index(local.hour)
        if window is None:
            continue
        measured.append((_daytype(local), window, local.hour, float(row.get("engagement") or 0.0)))

    slots: list[Slot] = []
    seen: set[datetime] = set()
    basis = "general"
    if len(measured) >= MIN_HISTORY:
        cells: dict[tuple[str, int], list[tuple[int, float]]] = {}
        for daytype, window, hour, engagement in measured:
            cells.setdefault((daytype, window), []).append((hour, engagement))
        ranked = sorted(
            ((statistics.median(e for _, e in rows), key, rows) for key, rows in cells.items() if len(rows) >= 2),
            key=lambda item: item[0], reverse=True,
        )
        for median, (daytype, window), rows in ranked:
            if median <= 0 or len(slots) >= MAX_SLOTS:
                continue
            by_hour: dict[int, list[float]] = {}
            for hour, engagement in rows:
                by_hour.setdefault(hour, []).append(engagement)
            best_hour = max(by_hour, key=lambda h: (statistics.median(by_hour[h]), len(by_hour[h])))
            at = _next_occurrence(now, zone, daytype, best_hour, after)
            if at is None or at in seen:
                continue
            seen.add(at)
            # How much better this slot did than every other post (the same comparison the app's pattern finder makes).
            rest = [e for d, w, _, e in measured if (d, w) != (daytype, window)]
            rest_median = statistics.median(rest) if rest else 0.0
            ratio = median / rest_median if rest_median > 0 else 1.0
            slots.append(Slot(
                at=at, confidence=_confidence(len(rows), ratio),
                reason=f"Your {label} posts get the most response {_phrase(daytype, window)}.",
                clash=_clash_note(at, planned, label, zone),
            ))
        if slots:
            basis = "history"

    if len(slots) < MAX_SLOTS:
        for daytype, hour in GENERAL_PATTERNS.get(platform, DEFAULT_PATTERN):
            if len(slots) >= MAX_SLOTS:
                break
            at = _next_occurrence(now, zone, daytype, hour, after)
            if at is None or at in seen:
                continue
            seen.add(at)
            slots.append(Slot(at=at, confidence="general", reason=f"A common high-traffic time for {label}.", clash=_clash_note(at, planned, label, zone)))

    # Slots without a clash come first, then by how well they are expected to do; a clash is shown, not hidden.
    order = {"high": 0, "medium": 1, "low": 2, "general": 3}
    slots.sort(key=lambda s: (s.clash is not None, order[s.confidence], s.at))
    note = None
    if basis == "general":
        note = f"You don't have enough results yet, so these come from general patterns for {label}. They get better as you post."
    return {"basis": basis, "slots": [s.to_dict() for s in slots[:MAX_SLOTS]], "note": note, "timezone": zone.key}
