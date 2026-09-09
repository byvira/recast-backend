"""
LinkedIn analytics fetcher.
Uses LinkedIn UGC Posts API for post metrics
and LinkedIn Profile API for account metrics.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.pipelines.analytics.base import AnalyticsFetcher, PostMetrics, AccountMetrics

logger = logging.getLogger(__name__)

LINKEDIN_BASE = "https://api.linkedin.com/v2"


class LinkedInAnalyticsFetcher(AnalyticsFetcher):

    platform = "linkedin"

    async def fetch_post_metrics(
        self,
        platform_post_id: str,
        platform_user_id: str,
        access_token: str,
        piece_id: str = "",
    ) -> PostMetrics:
        """Fetch social actions for a LinkedIn post."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                headers = {"Authorization": f"Bearer {access_token}"}

                # Social actions (likes, comments, shares)
                social_resp = await client.get(
                    f"{LINKEDIN_BASE}/socialActions/{platform_post_id}",
                    headers=headers,
                )

                likes = comments = shares = 0

                if social_resp.status_code == 200:
                    data     = social_resp.json()
                    likes    = data.get("likesSummary", {}).get("totalLikes", 0)
                    comments = data.get("commentsSummary", {}).get("totalFirstLevelComments", 0)
                    shares   = data.get("sharesSummary", {}).get("totalShares", 0)

                # Post statistics — impressions, clicks
                stats_resp = await client.get(
                    f"{LINKEDIN_BASE}/organizationalEntityShareStatistics",
                    params={
                        "q":     "organizationalEntity",
                        "ugcPosts[0]": platform_post_id,
                    },
                    headers=headers,
                )

                impressions = clicks = 0

                if stats_resp.status_code == 200:
                    elements = stats_resp.json().get("elements", [])
                    if elements:
                        stats       = elements[0].get("totalShareStatistics", {})
                        impressions = stats.get("impressionCount", 0)
                        clicks      = stats.get("clickCount", 0)

                return PostMetrics(
                    platform=self.platform,
                    post_id=piece_id,
                    platform_post_id=platform_post_id,
                    likes=likes,
                    comments=comments,
                    shares=shares,
                    impressions=impressions,
                    clicks=clicks,
                    reach=impressions,
                    engagement_rate=self._compute_engagement_rate(likes, comments, shares, impressions),
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("LinkedIn post metrics failed for %s: %s", platform_post_id, exc)
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
        """Fetch LinkedIn profile follower count."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                headers = {"Authorization": f"Bearer {access_token}"}

                profile_resp = await client.get(
                    f"{LINKEDIN_BASE}/me",
                    params={"fields": "id,localizedFirstName,localizedLastName"},
                    headers=headers,
                )
                profile_resp.raise_for_status()
                profile = profile_resp.json()

                username = (
                    f"{profile.get('localizedFirstName', '')} "
                    f"{profile.get('localizedLastName', '')}".strip()
                )

                # Follower stats
                follower_resp = await client.get(
                    f"{LINKEDIN_BASE}/networkSizes/{platform_user_id}",
                    params={"edgeType": "CompanyFollowedByMember"},
                    headers=headers,
                )

                followers = 0
                if follower_resp.status_code == 200:
                    followers = follower_resp.json().get("firstDegreeSize", 0)

                return AccountMetrics(
                    platform=self.platform,
                    platform_user_id=platform_user_id,
                    username=username,
                    followers=followers,
                    period_start=since,
                    period_end=until,
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("LinkedIn account metrics failed for %s: %s", platform_user_id, exc)
            return AccountMetrics(
                platform=self.platform,
                platform_user_id=platform_user_id,
                fetched_at=datetime.now(timezone.utc),
            )