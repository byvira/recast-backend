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
    # The earlier image work measured 10,000 neurons a day at about 57.6 neurons a picture, so about 173 pictures a day
    # for the whole app. That is from the code notes, not checked against Cloudflare today.
    "cloudflare": {"@cf/black-forest-labs/flux-1-schnell": {"rpd": 173}},
    "elevenlabs": {},  # credits are monthly and counted in characters; not modelled here
    "deepgram": {},
    "nvidia": {},
    "mistral": {},  # the owner enters these from the provider's own limits page; none are invented here
    "openrouter": {},
    "huggingface": {},
    "pollinations": {},
}
DEFAULT_RESET = {
    "groq": {"tz": "UTC", "hour": 0},
    "gemini": {"tz": "America/Los_Angeles", "hour": 0},  # Google resets daily requests at midnight Pacific
    "cloudflare": {"tz": "UTC", "hour": 0},
    "elevenlabs": {"tz": "UTC", "hour": 0},
    "deepgram": {"tz": "UTC", "hour": 0},
    "mistral": {"tz": "UTC", "hour": 0},
    "nvidia": {"tz": "UTC", "hour": 0},
    "openrouter": {"tz": "UTC", "hour": 0},
    "huggingface": {"tz": "UTC", "hour": 0},
    "pollinations": {"tz": "UTC", "hour": 0},
}

# Every AI provider the product calls, with a plain description of what it is used for. A provider shows on the page
# as soon as it is listed here, even before it has had any traffic.
PROVIDERS: dict[str, dict[str, str]] = {
    "groq": {"label": "Groq", "used_for": "Writing, hooks, analysis and speech to text"},
    "gemini": {"label": "Gemini", "used_for": "Backup writing, images, video understanding and voice matching"},
    "cloudflare": {"label": "Cloudflare Workers AI", "used_for": "Picture generation"},
    "elevenlabs": {"label": "ElevenLabs", "used_for": "Voice narration and echo reduction"},
    "deepgram": {"label": "Deepgram", "used_for": "Voice narration when ElevenLabs is not available"},
    "nvidia": {"label": "NVIDIA", "used_for": "Third backup writing, on free hosted models"},
    "mistral": {"label": "Mistral", "used_for": "Backup writing when Groq and Gemini are not answering"},
    "openrouter": {"label": "OpenRouter", "used_for": "Second backup writing, on free open models"},
    "huggingface": {"label": "Hugging Face", "used_for": "Backup pictures when Cloudflare and Gemini are not answering"},
    "pollinations": {"label": "Pollinations", "used_for": "Last backup for pictures (free, no key)"},
}

# The setting that holds each provider's key. A provider whose key is empty is "not set up", not "down".
KEY_SETTING = {
    "groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY", "cloudflare": "CLOUDFLARE_API_TOKEN", "elevenlabs": "ELEVENLABS_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY", "mistral": "MISTRAL_API_KEY", "nvidia": "NVIDIA_API_KEY", "openrouter": "OPENROUTER_API_KEY", "huggingface": "HUGGINGFACE_API_TOKEN",
}


def is_configured(provider: str) -> bool:
    from app.core.config import settings

    if provider == "elevenlabs" and not settings.ELEVENLABS_ENABLED:
        return False  # switched off on purpose
    if provider == "pollinations":
        return bool(settings.POLLINATIONS_ENABLED)  # needs no key
    name = KEY_SETTING.get(provider)
    return True if name is None else bool(getattr(settings, name, ""))


# Where the starting numbers came from, shown next to them. Anything not listed here was entered by the owner.
DEFAULT_SOURCE = {"cloudflare": "from the code notes (10,000 neurons a day, about 58 per picture), not verified"}
OWNER_SOURCE = "entered by owner, not verified"


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
