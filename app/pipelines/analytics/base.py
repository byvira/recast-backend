"""
Analytics base — shared models and abstract fetcher.
Every platform fetcher inherits from AnalyticsFetcher and returns
PostMetrics / AccountMetrics so the aggregator can unify them.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ── Post-level metrics ────────────────────────────────────────────────────────

class PostMetrics(BaseModel):
    """Engagement metrics for a single published post."""

    platform:         str
    post_id:          str
    platform_post_id: str

    # Engagement
    likes:       int = 0
    comments:    int = 0
    shares:      int = 0
    reposts:     int = 0
    saves:       int = 0
    clicks:      int = 0

    # Reach
    impressions: int = 0
    reach:       int = 0
    views:       int = 0

    # Computed
    engagement_rate: float = 0.0   # (likes+comments+shares) / reach * 100

    fetched_at: datetime = Field(default_factory=datetime.utcnow)


# ── Account-level metrics ─────────────────────────────────────────────────────

class AccountMetrics(BaseModel):
    """Overall account health metrics for a platform."""

    platform:          str
    platform_user_id:  str
    username:          str = ""

    # Audience
    followers:         int = 0
    following:         int = 0
    follower_delta:    int = 0   # change since last fetch

    # Reach (period)
    total_impressions: int = 0
    total_reach:       int = 0
    profile_views:     int = 0

    # Content
    total_posts:       int = 0

    # Period
    period_start: Optional[datetime] = None
    period_end:   Optional[datetime] = None

    fetched_at: datetime = Field(default_factory=datetime.utcnow)


# ── Base fetcher ──────────────────────────────────────────────────────────────

class AnalyticsFetcher(ABC):
    """
    Abstract base for platform analytics fetchers.
    Each platform implements fetch_post_metrics and fetch_account_metrics.
    """

    platform: str = ""

    @abstractmethod
    async def fetch_post_metrics(
        self,
        platform_post_id: str,
        platform_user_id: str,
        access_token: str,
        piece_id: str = "",
    ) -> PostMetrics:
        """Fetch engagement metrics for a single post."""
        ...

    @abstractmethod
    async def fetch_account_metrics(
        self,
        platform_user_id: str,
        access_token: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> AccountMetrics:
        """Fetch account-level metrics for a given period."""
        ...

    def _compute_engagement_rate(
        self,
        likes: int,
        comments: int,
        shares: int,
        reach: int,
    ) -> float:
        """Engagement rate = (likes + comments + shares) / reach * 100."""
        if reach == 0:
            return 0.0
        return round((likes + comments + shares) / reach * 100, 2)