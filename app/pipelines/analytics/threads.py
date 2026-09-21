"""
Threads analytics fetcher.
Uses Threads Graph API — threads_basic scope covers insights.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.pipelines.analytics.base import AnalyticsFetcher, PostMetrics, AccountMetrics

logger = logging.getLogger(__name__)

THREADS_BASE = "https://graph.threads.net/v1.0"


class ThreadsAnalyticsFetcher(AnalyticsFetcher):

    platform = "threads"

    async def fetch_post_metrics(
        self,
        platform_post_id: str,
        platform_user_id: str,
        access_token: str,
        piece_id: str = "",
    ) -> PostMetrics:
        """Fetch insights for a single Threads post."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{THREADS_BASE}/{platform_post_id}/insights",
                    params={
                        "metric":       "likes,replies,reposts,quotes,views",
                        "access_token": access_token,
                    },
                )
                resp.raise_for_status()

                metrics = {
                    item["name"]: item.get("values", [{}])[0].get("value", 0)
                    for item in resp.json().get("data", [])
                }

                likes    = metrics.get("likes", 0)
                comments = metrics.get("replies", 0)
                reposts  = metrics.get("reposts", 0)
                views    = metrics.get("views", 0)

                return PostMetrics(
                    platform=self.platform,
                    post_id=piece_id,
                    platform_post_id=platform_post_id,
                    likes=likes,
                    comments=comments,
                    reposts=reposts,
                    views=views,
                    reach=views,
                    engagement_rate=self._compute_engagement_rate(likes, comments, reposts, views),
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("Threads post metrics failed for %s: %s", platform_post_id, exc)
            return PostMetrics(
                platform=self.platform,
                post_id=piece_id,
                platform_post_id=platform_post_id,
                fetched_at=datetime.now(timezone.utc),
            )

    async def fetch_account_metrics(
        self,
        platform_user_id: str,
        access_token: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> AccountMetrics:
        """Fetch Threads account-level insights."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:

                # Profile — followers_count is not a valid field on this
                # plain object endpoint (it's only exposed via
                # /threads_insights below), and threads_count doesn't exist
                # at all ("Tried accessing nonexisting field", confirmed via
                # a raw API call) — requesting either was causing a 500 from
                # Threads' API on every real connected account. total_posts
                # is left at 0 — there is no real field for it on this node.
                profile_resp = await client.get(
                    f"{THREADS_BASE}/{platform_user_id}",
                    params={
                        "fields":       "id,username",
                        "access_token": access_token,
                    },
                )
                profile_resp.raise_for_status()
                profile = profile_resp.json()

                # Account insights
                insights_resp = await client.get(
                    f"{THREADS_BASE}/{platform_user_id}/threads_insights",
                    params={
                        "metric":       "views,likes,replies,reposts,quotes,followers_count",
                        "period":       "day",
                        "access_token": access_token,
                    },
                )

                total_views = total_likes = followers = 0

                if insights_resp.status_code == 200:
                    for item in insights_resp.json().get("data", []):
                        name  = item.get("name")
                        value = item.get("total_value", {}).get("value", 0)
                        if name == "views":            total_views = value
                        if name == "likes":            total_likes = value
                        if name == "followers_count":  followers   = value
                else:
                    # threads_insights (unlike the plain profile fetch above)
                    # requires the threads_manage_insights scope — see
                    # connect_threads() in app/api/v1/oauth.py. A workspace
                    # connected before that scope was requested needs to
                    # reconnect Threads for this to stop 4xx-ing.
                    logger.warning(
                        "Threads account insights unavailable for %s — %d: %s",
                        platform_user_id, insights_resp.status_code, insights_resp.text[:300],
                    )

                return AccountMetrics(
                    platform=self.platform,
                    platform_user_id=platform_user_id,
                    username=profile.get("username", ""),
                    followers=profile.get("followers_count", followers),
                    total_impressions=total_views,
                    total_reach=total_views,
                    total_posts=profile.get("threads_count", 0),
                    period_start=since,
                    period_end=until,
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("Threads account metrics failed for %s: %s", platform_user_id, exc)
            return AccountMetrics(
                platform=self.platform,
                platform_user_id=platform_user_id,
                fetched_at=datetime.now(timezone.utc),
            )