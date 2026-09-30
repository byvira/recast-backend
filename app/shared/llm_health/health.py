"""Provider status, quota headroom, the overall banner and the starting limits. Pure functions: the API
feeds them numbers read from the database, so the rules are tested without one.

The starting limits are the owner's figures for the free plans (entered 2026-10-02, not verified against
the providers). They are only defaults: staff edit them in settings, and the source is shown as
"entered by owner". Do not treat them as provider facts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

WARN_PCT = 70.0
CRITICAL_PCT = 90.0
STATUS_WINDOW_MIN = 5

# provider -> model -> limits. rpm/rpd = requests per minute/day, tpm/tpd = tokens per minute/day.
DEFAULT_LIMITS: dict[str, dict[str, dict[str, int]]] = {
    "groq": {
        "openai/gpt-oss-120b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000},
        "openai/gpt-oss-20b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000},
        "whisper-large-v3": {"rpm": 20, "rpd": 2000},
    },
    "gemini": {},  # Google publishes no per-model numbers to copy; the owner enters them from the AI Studio rate limit page
}
DEFAULT_RESET = {
    "groq": {"tz": "UTC", "hour": 0},
    "gemini": {"tz": "America/Los_Angeles", "hour": 0},  # Google resets daily requests at midnight Pacific
}


def last_reset(now: datetime, rule: dict[str, Any]) -> datetime:
    """The most recent moment the daily allowance reset, in UTC."""
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(rule.get("tz", "UTC"))
    except Exception:  # noqa: BLE001 - a missing time zone database falls back to UTC
        zone = timezone.utc
    local = now.astimezone(zone)
    boundary = local.replace(hour=int(rule.get("hour", 0)), minute=0, second=0, microsecond=0)
    if boundary > local:
        boundary -= timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def next_reset(now: datetime, rule: dict[str, Any]) -> datetime:
    start = last_reset(now, rule)
    for days in (1, 2):  # two tries so a daylight saving change cannot leave it in the past
        candidate = start + timedelta(days=days)
        if candidate > now:
            return candidate
    return start + timedelta(days=1)


def percent(used: float, limit: float | None) -> float | None:
    if not limit:
        return None
    return round(min(used / limit * 100, 999.0), 1)


def level(pct: float | None, warn: float = WARN_PCT, critical: float = CRITICAL_PCT) -> str:
    if pct is None:
        return "unknown"
    return "critical" if pct >= critical else "warning" if pct >= warn else "fine"


def projection(used: float, limit: float | None, window_start: datetime, now: datetime, reset_at: datetime) -> dict[str, Any] | None:
    """When the allowance runs out at today's pace, if that is before it resets."""
    if not limit or used <= 0 or reset_at <= now:
        return None
    elapsed = (now - window_start).total_seconds()
    if elapsed < 600:  # too little data to say anything honest
        return None
    rate = used / elapsed
    remaining = limit - used
    if remaining <= 0:
        return {"runs_out_at": now, "before_reset": True}
    at = now + timedelta(seconds=remaining / rate)
    return {"runs_out_at": at, "before_reset": at < reset_at}


def quota_rows(limits: dict[str, int], used: dict[str, float], *, warn: float, critical: float) -> list[dict[str, Any]]:
    """One row per day window that has a limit: requests and tokens."""
    rows = []
    for key, label, used_key in (
        ("rpd", "Requests today", "calls"), ("tpd", "Tokens today", "tokens"),
        ("rpm", "Requests this minute", "calls_min"), ("tpm", "Tokens this minute", "tokens_min"),
    ):
        limit = limits.get(key)
        if not limit:
            continue
        pct = percent(used.get(used_key, 0), limit)
        rows.append({"window": key, "label": label, "limit": limit, "used": int(used.get(used_key, 0)), "percent": pct, "level": level(pct, warn, critical)})
    return rows


def provider_status(
    *, calls: int, successes: int, quota_levels: list[str], open_issue_priorities: list[str], auth_failed: bool, limit_reached: bool, fallback_used: bool,
) -> str:
    """idle | healthy | degraded | limit_reached | down, from the last few minutes."""
    if auth_failed:
        return "down"
    if calls == 0:
        return "idle"
    rate = successes / calls
    if rate < 0.5:
        return "down"
    if limit_reached:
        return "limit_reached"
    if rate < 0.98 or "critical" in quota_levels or fallback_used or "critical" in open_issue_priorities:
        return "degraded"
    return "healthy"


STATUS_TEXT = {
    "healthy": "Working", "degraded": "Slower or partly failing", "limit_reached": "Limit reached", "down": "Not working", "idle": "No recent calls",
}


def overall(providers: dict[str, str], *, user_facing_failures: bool, top_issue_title: str | None) -> dict[str, str]:
    """The banner: all_working | partly_working | not_working, with one plain sentence."""
    states = [s for s in providers.values() if s != "idle"]
    # People seeing failures comes first: a provider can look idle or healthy while an open issue says otherwise.
    if user_facing_failures:
        why = f" {top_issue_title}." if top_issue_title else ""
        return {"state": "not_working", "text": f"Not working. People are seeing failures.{why}"}
    if not states or all(s == "healthy" for s in states):
        return {"state": "all_working", "text": "All working."}
    why = f" {top_issue_title}." if top_issue_title else ""
    return {"state": "partly_working", "text": f"Partly working. Generation still succeeds through the fallback.{why}"}
