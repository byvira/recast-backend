"""
Bluesky analytics fetcher.
No official analytics API — metrics are calculated from post record data
via the AT Protocol public API.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.pipelines.analytics.base import AnalyticsFetcher, PostMetrics, AccountMetrics

logger = logging.getLogger(__name__)

BSKY_BASE = "https://public.api.bsky.app/xrpc"


class BlueskyAnalyticsFetcher(AnalyticsFetcher):

    platform = "bluesky"

    async def fetch_post_metrics(
        self,
        platform_post_id: str,
        platform_user_id: str,
        access_token: str,
        piece_id: str = "",
    ) -> PostMetrics:
        """
        Fetch metrics for a Bluesky post.
        platform_post_id should be the AT URI (at://did:plc:.../app.bsky.feed.post/...)
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{BSKY_BASE}/app.bsky.feed.getPosts",
                    params={"uris": platform_post_id},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                resp.raise_for_status()

                posts = resp.json().get("posts", [])
                if not posts:
                    logger.warning("Bluesky post not found: %s", platform_post_id)
                    return PostMetrics(
                        platform=self.platform,
                        post_id=piece_id,
                        platform_post_id=platform_post_id,
                        fetched_at=datetime.now(timezone.utc),
                    )

                post     = posts[0]
                likes    = post.get("likeCount",   0)
                reposts  = post.get("repostCount", 0)
                comments = post.get("replyCount",  0)
                views    = post.get("quoteCount",  0)

                reach = likes + reposts + comments

                return PostMetrics(
                    platform=self.platform,
                    post_id=piece_id,
                    platform_post_id=platform_post_id,
                    likes=likes,
                    reposts=reposts,
                    comments=comments,
                    reach=reach,
                    engagement_rate=self._compute_engagement_rate(likes, comments, reposts, reach),
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("Bluesky post metrics failed for %s: %s", platform_post_id, exc)
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
        """Fetch Bluesky profile stats."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{BSKY_BASE}/app.bsky.actor.getProfile",
                    params={"actor": platform_user_id},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                resp.raise_for_status()
                profile = resp.json()

                return AccountMetrics(
                    platform=self.platform,
                    platform_user_id=platform_user_id,
                    username=profile.get("handle", ""),
                    followers=profile.get("followersCount", 0),
                    following=profile.get("followsCount",   0),
                    total_posts=profile.get("postsCount",   0),
                    period_start=since,
                    period_end=until,
                    fetched_at=datetime.now(timezone.utc),
                )

        except Exception as exc:
            logger.error("Bluesky account metrics failed for %s: %s", platform_user_id, exc)
            return AccountMetrics(
                platform=self.platform,
                platform_user_id=platform_user_id,
                fetched_at=datetime.now(timezone.utc),
            )