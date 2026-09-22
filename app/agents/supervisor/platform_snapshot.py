"""
Platform-aware facts for the reasoning pass's digest — the concrete mechanism
behind docs/PLATFORM_REGISTRY_PLAN.md Stage 3's "not just a logs giver" goal.

Per the plan's second hard rule, this deliberately gathers two distinct kinds
of fact and keeps them visibly separate in the returned dict, not merged into
one blob:
  1. Platform-level behavior (global, from the registry: tone_profile,
     policy_constraints, integration_pattern) — true regardless of our usage.
  2. Our usage of it (per-workspace: connections, real post_metrics/
     account_metrics) — what actually happened.
A recommendation that only reads (2) and ignores (1) risks suggesting
something the platform's own rules forbid (e.g. identical cross-subreddit
Reddit posts) — see the plan's Reddit example.

Cheap and pre-aggregated on purpose, same philosophy as digest.py: the
reasoning pass gets a compact summary, not a raw metrics dump — it can still
reach for get_recent_publishes / get_member_recent_content (tools.py) if it
needs more detail on a specific platform.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.mongo import account_metrics, post_metrics, workspace_connections
from app.platforms.base import get_platform, import_all

PERFORMANCE_LOOKBACK_DAYS = 30


async def gather_platform_snapshot(workspace_id: str) -> dict:
    import_all()

    connections = await workspace_connections.find(
        {"workspace_id": workspace_id, "is_active": True}
    ).to_list(length=100)

    since = datetime.now(timezone.utc) - timedelta(days=PERFORMANCE_LOOKBACK_DAYS)
    posts = await post_metrics.find(
        {"workspace_id": workspace_id, "fetched_at": {"$gte": since}}
    ).to_list(length=2000)
    accounts = await account_metrics.find({"workspace_id": workspace_id}).to_list(length=50)
    accounts_by_platform = {a.get("platform", ""): a for a in accounts}

    posts_by_platform: dict[str, list[dict]] = {}
    for pm in posts:
        posts_by_platform.setdefault(pm.get("platform", ""), []).append(pm)

    connected: list[dict] = []
    performance: dict[str, dict] = {}
    registry_notes: dict[str, dict] = {}

    for conn in connections:
        key = conn.get("platform", "")
        definition = get_platform(key)
        connected.append({
            "platform": key,
            "registry_status": definition.status if definition else "unregistered",
        })
        if definition:
            registry_notes[key] = {
                "label": definition.label,
                "tone_profile": definition.tone_profile,
                "policy_constraints": definition.policy_constraints,
                "integration_pattern": definition.integration_pattern,
            }

        acct = accounts_by_platform.get(key)
        plat_posts = posts_by_platform.get(key, [])
        if not acct and not plat_posts:
            continue
        rates = [p.get("engagement_rate", 0.0) or 0.0 for p in plat_posts]
        performance[key] = {
            "followers": acct.get("followers") if acct else None,
            "post_count": len(plat_posts),
            "avg_engagement_rate": round(sum(rates) / len(rates), 2) if rates else None,
        }

    return {
        "connected_platforms": connected,
        "platform_performance": performance,
        "platform_registry_notes": registry_notes,
        "window_days": PERFORMANCE_LOOKBACK_DAYS,
    }
