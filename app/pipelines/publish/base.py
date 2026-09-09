"""
Abstract base class for all platform publishers.
Every platform implements this same 5-method contract.
The pipeline never changes — adding a platform means
creating one class and adding one line to the registry.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class PublishRequest:
    """Everything a publisher needs to post content."""
    piece_id: str
    user_id: str
    brand_id: str
    platform: str
    content: str
    media_urls: list[str] = None
    platform_user_id: str = ""   
    scheduled_at: Optional[datetime] = None


@dataclass
class PublishResult:
    """Standardised result from any platform publisher."""
    success: bool
    platform: str
    piece_id: str
    platform_post_id: Optional[str] = None   # ID of the post on the platform
    platform_post_url: Optional[str] = None  # URL of the published post
    error_type: Optional[str] = None         # TRANSIENT / FIXABLE / AUTH / FATAL
    error_code: Optional[int] = None
    error_message: Optional[str] = None
    retry_after: Optional[int] = None        # seconds to wait before retry


class PlatformPublisher(ABC):
    """
    Abstract base class — all platforms implement this contract.
    Five methods. Every publisher, every platform.
    """

    @abstractmethod
    def build_auth_url(self, state: str) -> str:
        """
        Return the OAuth URL to redirect the user to.
        State parameter used to prevent CSRF.
        """
        ...

    @abstractmethod
    async def exchange_token(self, code: str) -> dict:
        """
        Exchange OAuth code for access + refresh tokens.
        Returns dict with: access_token, refresh_token, expires_at,
        platform_user_id, username.
        """
        ...

    @abstractmethod
    async def refresh_token(self, refresh_token: str) -> dict:
        """
        Refresh an expiring access token.
        Returns dict with: access_token, expires_at.
        """
        ...

    @abstractmethod
    async def publish(
        self,
        request: PublishRequest,
        access_token: str,
    ) -> PublishResult:
        """
        Post content to the platform.
        Returns PublishResult with success/failure details.
        """
        ...

    @abstractmethod
    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        """
        Validate content against platform rules before publishing.
        Returns (is_valid, list_of_issues).
        Called between generation and publishing.
        """
        ...