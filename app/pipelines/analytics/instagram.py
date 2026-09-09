"""
Instagram analytics fetcher.
Uses Facebook Graph API — same token as the publisher.
Requires instagram_basic + instagram_manage_insights scopes.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.pipelines.analytics.base import AnalyticsFetcher, PostMetrics, AccountMetrics

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v25.0"


class InstagramAnalyticsFetcher(AnalyticsFetcher):

    platform = "instagram"

    async def fetch_post_metrics(
        self,
        platform_post_id: str,
        platform_user_id: str,
        access_token: str,
        piece_id: str = "",
    ) -> PostMetrics:
        """Fetch insights for a single Instagram media object."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:

                # Basic counts — likes, comments
                media_resp = await client.get(
                    f"{GRAPH_BASE}/{platform_post_id}",
                    params={
                        "fields":       "like_count,comments_count",
                        "access_token": access_token,
                    },
                )
                media_resp.raise_for_status()
                media = media_resp.json()

                likes    = media.get("like_count", 0)
                comments = media.get("comments_count", 0)

                # Insights — reach, impressions, saves
                insights_resp = await client.get(
                    f"{GRAPH_BASE}/{platform_post_id}/insights",
                    params={
                        "metric":       "impressions,reach,saved",
                        "access_token": access_token,
                    },
                )
                impressions = saves = reach = 0

                if insights_resp.status_code == 200:
                    for item in insights_resp.json().get("data", []):
                        name  = item.get("name")
                        value = item.get("values", [{}])[0].get("value", 0)
                        if name == "impressions": impressions = value
                        if name == "reach":       reach       = value
                        if name == "saved":       saves       = value
                else:
                    logger.warning(
                        "Instagram insights unavailable for post %s — %d",
                        platform_post_id, insights_resp.status_code,
                    )

                return PostMetrics(
                    platform=self.platform,
                    post_id=piece_id,
                    platform_post_id=platform_post_id,
                    likes=likes,
                    comments=comments,
                    saves=saves,
                    impressions=impressions,
                    reach=reach,
                    engagement_rate=self._compute_engagement_rate(likes, comments, 0, reach),
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("Instagram post metrics failed for %s: %s", platform_post_id, exc)
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
        """Fetch Instagram account-level insights."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:

                # Follower count + media count
                profile_resp = await client.get(
                    f"{GRAPH_BASE}/{platform_user_id}",
                    params={
                        "fields":       "followers_count,follows_count,media_count,username",
                        "access_token": access_token,
                    },
                )
                profile_resp.raise_for_status()
                profile = profile_resp.json()

                # Account insights — impressions, reach, profile_views
                insights_resp = await client.get(
                    f"{GRAPH_BASE}/{platform_user_id}/insights",
                    params={
                        "metric":  "impressions,reach,profile_views",
                        "period":  "day",
                        "access_token": access_token,
                    },
                )

                impressions = reach = profile_views = 0

                if insights_resp.status_code == 200:
                    for item in insights_resp.json().get("data", []):
                        name  = item.get("name")
                        total = sum(v.get("value", 0) for v in item.get("values", []))
                        if name == "impressions":   impressions   = total
                        if name == "reach":         reach         = total
                        if name == "profile_views": profile_views = total

                return AccountMetrics(
                    platform=self.platform,
                    platform_user_id=platform_user_id,
                    username=profile.get("username", ""),
                    followers=profile.get("followers_count", 0),
                    following=profile.get("follows_count", 0),
                    total_impressions=impressions,
                    total_reach=reach,
                    profile_views=profile_views,
                    total_posts=profile.get("media_count", 0),
                    period_start=since,
                    period_end=until,
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("Instagram account metrics failed for %s: %s", platform_user_id, exc)
            return AccountMetrics(
                platform=self.platform,
                platform_user_id=platform_user_id,
                fetched_at=datetime.now(timezone.utc),
            )