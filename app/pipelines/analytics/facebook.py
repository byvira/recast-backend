"""
Facebook analytics fetcher.
Uses Meta Graph API — Page access token.
platform_user_id is the Facebook Page ID.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.pipelines.analytics.base import AnalyticsFetcher, PostMetrics, AccountMetrics

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v25.0"


class FacebookAnalyticsFetcher(AnalyticsFetcher):

    platform = "facebook"

    async def fetch_post_metrics(
        self,
        platform_post_id: str,
        platform_user_id: str,
        access_token: str,
        piece_id: str = "",
    ) -> PostMetrics:
        """
        Fetch insights for a single Facebook Page post.
        platform_post_id format: {page_id}_{post_id}
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:

                # Basic reactions + comments + shares
                post_resp = await client.get(
                    f"{GRAPH_BASE}/{platform_post_id}",
                    params={
                        "fields":       "reactions.summary(true),comments.summary(true),shares",
                        "access_token": access_token,
                    },
                )
                post_resp.raise_for_status()
                post = post_resp.json()

                likes    = post.get("reactions", {}).get("summary", {}).get("total_count", 0)
                comments = post.get("comments",  {}).get("summary", {}).get("total_count", 0)
                shares   = post.get("shares",    {}).get("count", 0)

                # Post insights — impressions, reach, clicks
                insights_resp = await client.get(
                    f"{GRAPH_BASE}/{platform_post_id}/insights",
                    params={
                        "metric":       "post_impressions,post_impressions_unique,post_clicks",
                        "access_token": access_token,
                    },
                )

                impressions = reach = clicks = 0

                if insights_resp.status_code == 200:
                    for item in insights_resp.json().get("data", []):
                        name  = item.get("name")
                        value = item.get("values", [{}])[0].get("value", 0)
                        if name == "post_impressions":        impressions = value
                        if name == "post_impressions_unique": reach       = value
                        if name == "post_clicks":             clicks      = value
                else:
                    logger.warning(
                        "Facebook post insights unavailable for %s — %d",
                        platform_post_id, insights_resp.status_code,
                    )

                return PostMetrics(
                    platform=self.platform,
                    post_id=piece_id,
                    platform_post_id=platform_post_id,
                    likes=likes,
                    comments=comments,
                    shares=shares,
                    impressions=impressions,
                    reach=reach,
                    clicks=clicks,
                    engagement_rate=self._compute_engagement_rate(likes, comments, shares, reach),
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("Facebook post metrics failed for %s: %s", platform_post_id, exc)
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
        """
        Fetch Facebook Page insights.
        platform_user_id is the Page ID.
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:

                # Page profile — name + fan count
                page_resp = await client.get(
                    f"{GRAPH_BASE}/{platform_user_id}",
                    params={
                        "fields":       "name,fan_count,followers_count",
                        "access_token": access_token,
                    },
                )
                page_resp.raise_for_status()
                page = page_resp.json()

                # Page insights — impressions, reach
                insights_resp = await client.get(
                    f"{GRAPH_BASE}/{platform_user_id}/insights",
                    params={
                        "metric":       "page_impressions,page_impressions_unique,page_views_total",
                        "period":       "day",
                        "access_token": access_token,
                    },
                )

                impressions = reach = profile_views = 0

                if insights_resp.status_code == 200:
                    for item in insights_resp.json().get("data", []):
                        name  = item.get("name")
                        total = sum(v.get("value", 0) for v in item.get("values", []))
                        if name == "page_impressions":        impressions   = total
                        if name == "page_impressions_unique": reach         = total
                        if name == "page_views_total":        profile_views = total
                else:
                    logger.warning(
                        "Facebook page insights unavailable for %s — %d",
                        platform_user_id, insights_resp.status_code,
                    )

                return AccountMetrics(
                    platform=self.platform,
                    platform_user_id=platform_user_id,
                    username=page.get("name", ""),
                    followers=page.get("followers_count", page.get("fan_count", 0)),
                    total_impressions=impressions,
                    total_reach=reach,
                    profile_views=profile_views,
                    period_start=since,
                    period_end=until,
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("Facebook account metrics failed for %s: %s", platform_user_id, exc)
            return AccountMetrics(
                platform=self.platform,
                platform_user_id=platform_user_id,
                fetched_at=datetime.now(timezone.utc),
            )