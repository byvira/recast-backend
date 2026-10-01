"""What the AI Credits panel says, worked out from what the workspace really did.

The panel used to show fixed numbers (74 percent, 9 days, four invented categories). Now every figure comes from saved data:
the token total and cap are the same ones that stop a run when the cap is reached (a rolling 30 days, see
`app.agents.supervisor.service.assert_ai_budget_available`), and the categories are counted from what the workspace made in
that same window. Image generation and voice do not spend AI tokens, so categories show what was made, not tokens, and the
panel says so. Nothing here is invented: with no activity every count is zero and the insight says so."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

WINDOW_DAYS = 30

# the four areas of the design, plus pictures, in the order they are shown
CATEGORY_LABELS: dict[str, tuple[str, str, str]] = {
    # key: (title, singular, plural) used in the line under the title ("12 clips generated")
    "voice_audio": ("Voice & Audio", "clip generated", "clips generated"),
    "video": ("Video & Clips", "video made", "videos made"),
    "posts": ("Posts & Threads", "post generated", "posts generated"),
    "images": ("Pictures & Graphics", "picture made", "pictures made"),
    "insights": ("Reports & Insights", "insight written", "insights written"),
}


def shares(counts: dict[str, int]) -> dict[str, int]:
    """Each category's whole-number share of everything made in the window. Zero activity gives zeros, not a made up split."""
    total = sum(counts.values())
    if total <= 0:
        return {k: 0 for k in counts}
    return {k: round(100 * v / total) for k, v in counts.items()}


def _days_between(a: date, b: date) -> int:
    return (b - a).days


def window_facts(rows: list[dict[str, Any]], today: Optional[date] = None) -> dict[str, Any]:
    """Token and call totals for the window, how many days it has been used for, and when the oldest day drops off.

    The cap is a rolling window, so there is no monthly reset date. What can be said truthfully is how many days until the
    oldest day of usage leaves the window (and so frees that room up)."""
    today = today or datetime.now(timezone.utc).date()
    used = [r for r in rows if int(r.get("tokens_used", 0) or 0) > 0]
    tokens = sum(int(r.get("tokens_used", 0) or 0) for r in rows)
    calls = sum(int(r.get("calls", 0) or 0) for r in rows)
    if not used:
        return {"tokens": tokens, "calls": calls, "active_days": 0, "oldest_drops_off_in_days": None, "daily_pace": 0.0}
    first = min(date.fromisoformat(str(r["date"])) for r in used)
    span = max(1, min(WINDOW_DAYS, _days_between(first, today) + 1))
    drops_in = max(0, WINDOW_DAYS - _days_between(first, today))
    return {"tokens": tokens, "calls": calls, "active_days": len(used), "oldest_drops_off_in_days": drops_in, "daily_pace": tokens / span}


def insight(*, cap: Optional[int], tokens: int, pace: float, counts: dict[str, int]) -> dict[str, str]:
    """One plain sentence about how the workspace is really using AI, most useful first: running out soon, close to the
    cap, where most of the work went, or (with nothing yet) how to start. `kind` lets the screen pick the icon colour."""
    if cap and tokens > 0:
        used = round(100 * tokens / cap)
        if tokens >= cap:
            return {"kind": "warn", "text": "You have reached your AI cap, so new text runs are paused. Raise it in Odette's Quotas tab, or wait for older usage to roll off."}
        projected = pace * WINDOW_DAYS
        if projected > cap and pace > 0:
            days = max(1, int((cap - tokens) / pace))
            return {"kind": "warn", "text": f"At this pace you will reach your cap in about {days} {'day' if days == 1 else 'days'}. Raise it in Odette's Quotas tab or ease off."}
        if used >= 70:
            return {"kind": "warn", "text": f"You have used {used}% of your AI cap in the last {WINDOW_DAYS} days. Raise it in Odette's Quotas tab if you need more room."}
    total = sum(counts.values())
    if total <= 0:
        return {"kind": "empty", "text": f"Nothing yet in the last {WINDOW_DAYS} days. Make a post, a recording or a picture and it shows up here."}
    top = max(counts, key=lambda k: counts[k])
    title = CATEGORY_LABELS[top][0]
    share = round(100 * counts[top] / total)
    return {"kind": "info", "text": f"{title} is where most of your AI work went in the last {WINDOW_DAYS} days ({share}% of what you made)."}


def category_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    pct = shares(counts)
    out = []
    for key, (title, singular, plural) in CATEGORY_LABELS.items():
        n = counts.get(key, 0)
        out.append({"key": key, "title": title, "count": n, "detail": f"{n} {singular if n == 1 else plural}", "share": pct.get(key, 0)})
    return out
