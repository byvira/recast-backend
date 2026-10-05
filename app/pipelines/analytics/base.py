"""
Analytics base — shared models and abstract fetcher.
Every platform fetcher inherits from AnalyticsFetcher and returns
PostMetrics / AccountMetrics so the aggregator can unify them.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


# ── Post-level metrics ────────────────────────────────────────────────────────

class PostMetrics(BaseModel):
    """Engagement metrics for a single published post."""

    workspace_id:     str = ""
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

    # False when the platform could not be read (error, timeout, no answer). The numbers are then NOT real: nothing
    # may store them, or a failed read would overwrite real figures with zeros.
    fetch_ok: bool = True
    # Why the platform could not be read, when it could not: "not_found" (the post is gone), "auth_error" (the connection
    # is refused) or "transient" (an error or timeout that may pass).
    failure: Optional[Literal["not_found", "auth_error", "transient"]] = None


# ── Account-level metrics ─────────────────────────────────────────────────────

class AccountMetrics(BaseModel):
    """Overall account health metrics for a platform."""

    workspace_id:      str = ""
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

def classify_failure(exc: BaseException) -> str:
    """Tells a post that is gone from a connection that is refused and from an error that may pass. Platforms report a
    deleted post in different ways: a 404, or a 400 with a "does not exist" body (Meta's error 100 with subcode 33,
    Bluesky's missing record). A refused token is a 401, or Meta's error 190."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        body = (response.text or "")[:600].lower() if response is not None else ""
    except Exception:  # noqa: BLE001
        body = ""
    text = f"{body} {str(exc).lower()}"
    if status in (404, 410):
        return "not_found"
    if status == 401 or '"code":190' in text.replace(" ", "") or "invalid oauth" in text or "token" in text and "expired" in text:
        return "auth_error"
    if status in (400, 403) and any(
        marker in text for marker in ("does not exist", "recordnotfound", "could not locate record", "not found", "has been deleted", "unsupported get request")
    ):
        return "not_found"
    return "transient"


def failure_from_status(*codes: int) -> str:
    """For a platform that answers with plain status codes: all of them 404 or 410 means the post is gone, any 401 means the
    connection is refused, anything else may pass."""
    if codes and all(c in (404, 410) for c in codes):
        return "not_found"
    if any(c == 401 for c in codes):
        return "auth_error"
    return "transient"
