"""The plans table: what each plan allows, as data.

A plan's limits are rules, so the numbers live in one place and Ops can change them without a code change. A rule is either a
number, or a seat-based rule for plans billed per seat:

    1200                                        a fixed number
    {"per_seat": 1000}                          1,000 for each seat
    {"per_seat": 10, "min": 30}                 10 for each seat, never fewer than 30
    {"base": 300, "per_seat_over": 20, "over": 10}
                                                300, plus 20 for each seat beyond the tenth

`None` means no limit. The values here are the defaults; a saved copy in the `billing_plans` collection (edited from Ops) wins
when present, read through `app.shared.entitlements`.
"""
from __future__ import annotations

from typing import Any, Optional, Union

Rule = Union[int, dict[str, int], None]

PLAN_IDS: tuple[str, ...] = ("free", "single", "duo", "large")

# Every limit a plan sets, in the order screens list them. unit: how the number reads; enforced_from: the stage that first
# checks it (the value is shown before then). kind "count" limits are checked when something is created; "period" limits
# reset (daily or monthly); "setting" limits shape behaviour rather than block anything.
LIMITS: dict[str, dict[str, str]] = {
    "seats": {"label": "Seats", "unit": "seats", "kind": "count"},
    "owned_workspaces": {"label": "Workspaces on this plan", "unit": "workspaces", "kind": "count"},
    "social_accounts": {"label": "Connected social accounts", "unit": "accounts", "kind": "count"},
    "brand_voices": {"label": "Brand voices", "unit": "voices", "kind": "count"},
    "credits_month": {"label": "Credits a month", "unit": "credits", "kind": "period"},
    "render_minutes_month": {"label": "Video minutes a month", "unit": "video minutes", "kind": "period"},
    "pictures_day": {"label": "AI pictures a day", "unit": "pictures", "kind": "period"},
    "storage_mb": {"label": "Storage", "unit": "MB", "kind": "count"},
    "scheduled_ahead": {"label": "Posts scheduled ahead", "unit": "posts", "kind": "count"},
    "campaigns_active": {"label": "Campaigns running at once", "unit": "campaigns", "kind": "count"},
    "video_automations": {"label": "Video automations", "unit": "automations", "kind": "count"},
    "api_keys": {"label": "API keys", "unit": "keys", "kind": "count"},
    "api_calls_per_minute": {"label": "Requests from connected apps a minute", "unit": "requests", "kind": "setting"},
    "version_history_days": {"label": "Version history kept", "unit": "days", "kind": "setting"},
    "analytics_history_days": {"label": "Analytics history kept", "unit": "days", "kind": "setting"},
    "review_link_days": {"label": "Review and share links last", "unit": "days", "kind": "setting"},
    "render_priority": {"label": "Video renders", "unit": "order", "kind": "setting"},
    "support_priority": {"label": "Support answered first", "unit": "yes or no", "kind": "setting"},
}

DEFAULT_PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "name": "Free",
        "per_seat": False,
        "min_seats": 1,
        "max_seats": 1,
        "limits": {
            "seats": 1, "owned_workspaces": 1, "social_accounts": 3, "brand_voices": 1, "credits_month": 100,
            "render_minutes_month": 5, "pictures_day": 5, "storage_mb": 1024, "scheduled_ahead": 30, "campaigns_active": 1,
            "video_automations": 1, "api_keys": 2, "api_calls_per_minute": 30, "version_history_days": 30,
            "analytics_history_days": 90, "review_link_days": 14, "render_priority": 0, "support_priority": 0,
        },
    },
    "single": {
        "name": "Single",
        "per_seat": False,
        "min_seats": 1,
        "max_seats": 1,
        "limits": {
            "seats": 1, "owned_workspaces": 3, "social_accounts": 10, "brand_voices": 3, "credits_month": 1200,
            "render_minutes_month": 60, "pictures_day": 50, "storage_mb": 10 * 1024, "scheduled_ahead": 500, "campaigns_active": 5,
            "video_automations": 5, "api_keys": 5, "api_calls_per_minute": 60, "version_history_days": 365,
            "analytics_history_days": 730, "review_link_days": 14, "render_priority": 1, "support_priority": 0,
        },
    },
    "duo": {
        "name": "Duo",
        "per_seat": False,
        "min_seats": 2,
        "max_seats": 2,
        "limits": {
            "seats": 2, "owned_workspaces": 5, "social_accounts": 20, "brand_voices": 6, "credits_month": 2500,
            "render_minutes_month": 150, "pictures_day": 100, "storage_mb": 30 * 1024, "scheduled_ahead": 1500,
            "campaigns_active": 10, "video_automations": 10, "api_keys": 10, "api_calls_per_minute": 120,
            "version_history_days": 365, "analytics_history_days": 730, "review_link_days": 14, "render_priority": 1,
            "support_priority": 0,
        },
    },
    "large": {
        "name": "Large",
        "per_seat": True,
        "min_seats": 3,
        "max_seats": 100,
        "limits": {
            "seats": {"per_seat": 1},
            "owned_workspaces": {"per_seat": 2, "min": 10},
            "social_accounts": {"per_seat": 10, "min": 30},
            "brand_voices": {"per_seat": 3, "min": 10},
            "credits_month": {"per_seat": 1000},
            "render_minutes_month": {"per_seat": 60},
            "pictures_day": {"per_seat": 30},
            "storage_mb": {"per_seat": 15 * 1024},
            "scheduled_ahead": {"per_seat": 500},
            "campaigns_active": {"per_seat": 3, "min": 10},
            "video_automations": {"per_seat": 3, "min": 10},
            "api_keys": {"base": 25, "per_seat_over": 2, "over": 10},
            "api_calls_per_minute": {"base": 300, "per_seat_over": 20, "over": 10},
            "version_history_days": 730,
            "analytics_history_days": 1095,
            "review_link_days": 14,
            "render_priority": 1,
            "support_priority": 1,
        },
    },
}


def resolve(rule: Rule, seats: int) -> Optional[int]:
    """The number a rule gives for this many seats. None stays None (no limit)."""
    if rule is None:
        return None
    if isinstance(rule, bool):
        return int(rule)
    if isinstance(rule, (int, float)):
        return int(rule)
    if "per_seat" in rule:
        return max(int(rule.get("min", 0)), int(rule["per_seat"]) * seats)
    if "base" in rule:
        over = int(rule.get("over", 0))
        return int(rule["base"]) + int(rule.get("per_seat_over", 0)) * max(0, seats - over)
    raise ValueError(f"Unknown limit rule: {rule!r}")


def clamp_seats(plan: dict[str, Any], seats: Optional[int]) -> int:
    """The seat count a plan bills for: the bought number within the plan's range, or its minimum."""
    low, high = int(plan.get("min_seats", 1)), int(plan.get("max_seats", 1))
    return min(high, max(low, int(seats or low)))


def limits_for(plan: dict[str, Any], seats: Optional[int] = None) -> dict[str, Optional[int]]:
    """Every limit of a plan as plain numbers, for the given seat count."""
    n = clamp_seats(plan, seats)
    rules = plan.get("limits") or {}
    return {key: resolve(rules.get(key), n) for key in LIMITS}


def validate_rule(rule: Any) -> Rule:
    """A rule as saved from Ops, checked: a whole number of zero or more, None, or one of the seat-based shapes."""
    if rule is None:
        return None
    if isinstance(rule, bool):
        raise ValueError("Use a number, not yes or no.")
    if isinstance(rule, int):
        if rule < 0:
            raise ValueError("A limit cannot be below zero.")
        return rule
    if isinstance(rule, dict):
        allowed = {"per_seat", "min", "base", "per_seat_over", "over"}
        if not rule or set(rule) - allowed:
            raise ValueError("A seat rule takes per_seat and min, or base, per_seat_over and over.")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in rule.values()):
            raise ValueError("Every number in a seat rule must be a whole number of zero or more.")
        if "per_seat" not in rule and "base" not in rule:
            raise ValueError("A seat rule needs per_seat or base.")
        return {k: int(v) for k, v in rule.items()}
    raise ValueError("A limit is a whole number, a seat rule, or empty for no limit.")
