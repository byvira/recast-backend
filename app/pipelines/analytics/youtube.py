"""
YouTube analytics fetcher.
Uses YouTube Data API v3 for video stats
and YouTube Analytics API for channel-level metrics.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.pipelines.analytics.base import AnalyticsFetcher, PostMetrics, AccountMetrics

logger = logging.getLogger(__name__)

YT_BASE       = "https://www.googleapis.com/youtube/v3"
YT_ANALYTICS  = "https://youtubeanalytics.googleapis.com/v2"


class YouTubeAnalyticsFetcher(AnalyticsFetcher):

    platform = "youtube"

    async def fetch_post_metrics(
        self,
        platform_post_id: str,
        platform_user_id: str,
        access_token: str,
        piece_id: str = "",
    ) -> PostMetrics:
        """Fetch stats for a single YouTube video."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                headers = {"Authorization": f"Bearer {access_token}"}

                resp = await client.get(
                    f"{YT_BASE}/videos",
                    params={
                        "part": "statistics",
                        "id":   platform_post_id,
                    },
                    headers=headers,
                )
                resp.raise_for_status()

                items = resp.json().get("items", [])
                stats = items[0].get("statistics", {}) if items else {}

                views    = int(stats.get("viewCount",    0))
                likes    = int(stats.get("likeCount",    0))
                comments = int(stats.get("commentCount", 0))

                return PostMetrics(
                    platform=self.platform,
                    post_id=piece_id,
                    platform_post_id=platform_post_id,
                    likes=likes,
                    comments=comments,
                    views=views,
                    reach=views,
                    impressions=views,
                    engagement_rate=self._compute_engagement_rate(likes, comments, 0, views),
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("YouTube post metrics failed for %s: %s", platform_post_id, exc)
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
        """Fetch YouTube channel statistics."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                headers = {"Authorization": f"Bearer {access_token}"}

                # Channel stats
                channel_resp = await client.get(
                    f"{YT_BASE}/channels",
                    params={
                        "part": "snippet,statistics",
                        "mine": "true",
                    },
                    headers=headers,
                )
                channel_resp.raise_for_status()

                items   = channel_resp.json().get("items", [])
                channel = items[0] if items else {}
                stats   = channel.get("statistics", {})
                snippet = channel.get("snippet", {})

                followers   = int(stats.get("subscriberCount", 0))
                total_posts = int(stats.get("videoCount",      0))
                total_views = int(stats.get("viewCount",       0))
                username    = snippet.get("title", "")

                return AccountMetrics(
                    platform=self.platform,
                    platform_user_id=platform_user_id,
                    username=username,
                    followers=followers,
                    total_posts=total_posts,
                    total_impressions=total_views,
                    total_reach=total_views,
                    period_start=since,
                    period_end=until,
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("YouTube account metrics failed for %s: %s", platform_user_id, exc)
            return AccountMetrics(
                platform=self.platform,
                platform_user_id=platform_user_id,
                fetched_at=datetime.now(timezone.utc),
            )