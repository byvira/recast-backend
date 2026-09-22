"""
Analytics aggregator.
Combines metrics from all connected platforms into one unified response.
Always persists fetched metrics to MongoDB — every caller gets DB storage for free.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.pipelines.analytics.base import PostMetrics, AccountMetrics, AnalyticsFetcher
from app.pipelines.publish.token_store import get_token
from app.platforms.base import get_platform, import_all, list_platforms
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

# Sourced from app.platforms.PLATFORM_REGISTRY — each PlatformDefinition's
# analytics_fetcher_cls is a dotted path to one of the fetcher classes
# (instagram.py, facebook.py, etc). Instances are cached here per platform
# (same singleton-per-platform behavior the old hardcoded _FETCHERS dict
# had) so a new platform's fetcher is declared once, in its
# PlatformDefinition, not duplicated in a second dict here.
_fetcher_instances: dict[str, AnalyticsFetcher] = {}


def _get_fetcher(platform: str) -> AnalyticsFetcher | None:
    if platform in _fetcher_instances:
        return _fetcher_instances[platform]
    import_all()
    definition = get_platform(platform)
    fetcher_cls = definition.resolve_analytics_fetcher_cls() if definition else None
    if fetcher_cls is None:
        return None
    instance = fetcher_cls()
    _fetcher_instances[platform] = instance
    return instance


def _default_analytics_platforms() -> list[str]:
    import_all()
    return [p.key for p in list_platforms() if p.analytics_fetcher_cls]


async def fetch_account_metrics_all(
    workspace_id: str,
    platforms: Optional[list[str]] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> list[AccountMetrics]:
    """
    Fetch account-level metrics for all connected platforms (or a subset).
    Always persists results to MongoDB account_metrics collection, scoped by workspace.
    """
    if platforms is None:
        platforms = _default_analytics_platforms()

    db      = get_db()
    results = []

    for platform in platforms:
        fetcher = _get_fetcher(platform)
        if not fetcher:
            continue

        token = await get_token(workspace_id, platform)
        if not token:
            logger.warning("No token for workspace %s platform %s — skipping", workspace_id, platform)
            continue

        metrics = await fetcher.fetch_account_metrics(
            platform_user_id=token["platform_user_id"],
            access_token=token["access_token"],
            since=since,
            until=until,
        )
        metrics.workspace_id = workspace_id
        results.append(metrics)

        # ── Persist to DB ─────────────────────────────────────────────
        await db["account_metrics"].update_one(
            {"workspace_id": workspace_id, "platform": platform},
            {"$set": {
                **metrics.model_dump(),
                "workspace_id": workspace_id,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        logger.debug("account_metrics saved — workspace=%s platform=%s", workspace_id, platform)

    return results


async def fetch_post_metrics_all(
    workspace_id: str,
    posts: list[dict],
) -> list[PostMetrics]:
    """
    Fetch post metrics for a list of published posts across all platforms.
    Always persists results to MongoDB post_metrics collection, scoped by workspace.

    posts: list of dicts with keys:
        - platform
        - platform_post_id
        - platform_user_id
        - piece_id (optional)
    """
    db      = get_db()
    results = []

    for post in posts:
        platform = post.get("platform", "")
        fetcher  = _get_fetcher(platform)

        if not fetcher:
            logger.warning("No analytics fetcher for platform: %s", platform)
            continue

        token = await get_token(workspace_id, platform)
        if not token:
            logger.warning("No token for workspace %s platform %s — skipping", workspace_id, platform)
            continue

        metrics = await fetcher.fetch_post_metrics(
            platform_post_id=post["platform_post_id"],
            platform_user_id=post.get("platform_user_id", ""),
            access_token=token["access_token"],
            piece_id=post.get("piece_id", ""),
        )
        metrics.workspace_id = workspace_id
        results.append(metrics)

        # ── Persist to DB ─────────────────────────────────────────────
        await db["post_metrics"].update_one(
            {
                "workspace_id":     workspace_id,
                "platform":         metrics.platform,
                "platform_post_id": metrics.platform_post_id,
            },
            {"$set": {
                **metrics.model_dump(),
                "workspace_id": workspace_id,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        logger.debug(
            "post_metrics saved — workspace=%s platform=%s post=%s",
            workspace_id, platform, metrics.platform_post_id,
        )

    return results


def summarize(
    account_metrics: list[AccountMetrics],
    post_metrics: list[PostMetrics],
) -> dict:
    """
    Build a unified summary across all platforms.
    Used by the supervisor agent and the analytics API endpoint.
    """
    total_followers   = sum(m.followers        for m in account_metrics)
    total_impressions = sum(m.total_impressions for m in account_metrics)
    total_reach       = sum(m.total_reach       for m in account_metrics)

    total_likes    = sum(m.likes              for m in post_metrics)
    total_comments = sum(m.comments           for m in post_metrics)
    total_shares   = sum(m.shares + m.reposts for m in post_metrics)

    # Best performing post
    best_post = max(post_metrics, key=lambda m: m.engagement_rate, default=None)

    # Per-platform breakdown
    platform_breakdown = {}
    for m in account_metrics:
        platform_breakdown[m.platform] = {
            "followers":   m.followers,
            "impressions": m.total_impressions,
            "reach":       m.total_reach,
            "posts":       m.total_posts,
        }

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "followers":   total_followers,
            "impressions": total_impressions,
            "reach":       total_reach,
            "likes":       total_likes,
            "comments":    total_comments,
            "shares":      total_shares,
        },
        "best_post": {
            "platform":         best_post.platform,
            "platform_post_id": best_post.platform_post_id,
            "engagement_rate":  best_post.engagement_rate,
            "likes":            best_post.likes,
            "comments":         best_post.comments,
        } if best_post else None,
        "platforms": platform_breakdown,
    }