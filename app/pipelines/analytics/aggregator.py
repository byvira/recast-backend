"""
Analytics aggregator.
Combines metrics from all connected platforms into one unified response.
Always persists fetched metrics to MongoDB — every caller gets DB storage for free.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.pipelines.analytics.base import PostMetrics, AccountMetrics
from app.pipelines.analytics.instagram import InstagramAnalyticsFetcher
from app.pipelines.analytics.threads import ThreadsAnalyticsFetcher
from app.pipelines.analytics.linkedin import LinkedInAnalyticsFetcher
from app.pipelines.analytics.youtube import YouTubeAnalyticsFetcher
from app.pipelines.analytics.bluesky import BlueskyAnalyticsFetcher
from app.pipelines.analytics.facebook import FacebookAnalyticsFetcher
from app.pipelines.publish.token_store import get_token
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

_FETCHERS = {
    "instagram": InstagramAnalyticsFetcher(),
    "facebook":  FacebookAnalyticsFetcher(),
    "threads":   ThreadsAnalyticsFetcher(),
    "linkedin":  LinkedInAnalyticsFetcher(),
    "youtube":   YouTubeAnalyticsFetcher(),
    "bluesky":   BlueskyAnalyticsFetcher(),
}


async def fetch_account_metrics_all(
    user_id: str,
    platforms: Optional[list[str]] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> list[AccountMetrics]:
    """
    Fetch account-level metrics for all connected platforms (or a subset).
    Always persists results to MongoDB account_metrics collection.
    """
    if platforms is None:
        platforms = list(_FETCHERS.keys())

    db      = get_db()
    results = []

    for platform in platforms:
        fetcher = _FETCHERS.get(platform)
        if not fetcher:
            continue

        token = await get_token(user_id, platform)
        if not token:
            logger.warning("No token for user %s platform %s — skipping", user_id, platform)
            continue

        metrics = await fetcher.fetch_account_metrics(
            platform_user_id=token["platform_user_id"],
            access_token=token["access_token"],
            since=since,
            until=until,
        )
        results.append(metrics)

        # ── Persist to DB ─────────────────────────────────────────────
        await db["account_metrics"].update_one(
            {"user_id": user_id, "platform": platform},
            {"$set": {
                **metrics.model_dump(),
                "user_id":    user_id,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        logger.debug("account_metrics saved — user=%s platform=%s", user_id, platform)

    return results


async def fetch_post_metrics_all(
    user_id: str,
    posts: list[dict],
) -> list[PostMetrics]:
    """
    Fetch post metrics for a list of published posts across all platforms.
    Always persists results to MongoDB post_metrics collection.

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
        fetcher  = _FETCHERS.get(platform)

        if not fetcher:
            logger.warning("No analytics fetcher for platform: %s", platform)
            continue

        token = await get_token(user_id, platform)
        if not token:
            logger.warning("No token for user %s platform %s — skipping", user_id, platform)
            continue

        metrics = await fetcher.fetch_post_metrics(
            platform_post_id=post["platform_post_id"],
            platform_user_id=post.get("platform_user_id", ""),
            access_token=token["access_token"],
            piece_id=post.get("piece_id", ""),
        )
        results.append(metrics)

        # ── Persist to DB ─────────────────────────────────────────────
        await db["post_metrics"].update_one(
            {
                "user_id":          user_id,
                "platform":         metrics.platform,
                "platform_post_id": metrics.platform_post_id,
            },
            {"$set": {
                **metrics.model_dump(),
                "user_id":    user_id,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        logger.debug(
            "post_metrics saved — user=%s platform=%s post=%s",
            user_id, platform, metrics.platform_post_id,
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