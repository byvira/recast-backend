"""One thin adapter that lets a config-driven platform (a webhook or a manual handoff) go through the same
Publish route, scheduled worker, retry and error-fixer path as the real publishers.

It does not sign in to anything: `uses_oauth_token` is False, so callers skip the token lookup. It returns the
same PublishResult every other publisher returns. For a manual handoff that result is never a success: it carries
the compose link and the worker keeps the post waiting for the member, because nothing was posted.
"""

from __future__ import annotations

from typing import Optional

from app.models.media import MediaAsset
from app.pipelines.publish.base import PublishRequest, PublishResult
from app.pipelines.publish.generic.manual_handoff_publisher import ManualHandoffPublisher
from app.pipelines.publish.generic.webhook_publisher import WebhookPublisher
from app.platforms.base import PlatformDefinition

ADAPTER_PATTERNS = ("token_webhook", "manual_handoff")


class NotApplicableForPattern(RuntimeError):
    """A sign-in step was asked of a platform that has no sign-in."""


def is_adapter_pattern(definition: Optional[PlatformDefinition]) -> bool:
    return bool(definition and definition.integration_pattern in ADAPTER_PATTERNS)


class ConfigPublisherAdapter:
    uses_oauth_token = False

    def __init__(self, definition: PlatformDefinition, config: dict):
        self.definition = definition
        self.config = config

    @property
    def is_manual(self) -> bool:
        return self.definition.integration_pattern == "manual_handoff"

    # Sign-in contract: these platforms have none.
    def build_auth_url(self, state: str) -> str:
        raise NotApplicableForPattern(f"{self.definition.label} does not use sign in.")

    async def exchange_token(self, code: str) -> dict:
        raise NotApplicableForPattern(f"{self.definition.label} does not use sign in.")

    async def refresh_token(self, refresh_token: str) -> dict:
        raise NotApplicableForPattern(f"{self.definition.label} does not use sign in.")

    def validate_content(self, content: str) -> tuple[bool, list[str]]:
        issues: list[str] = []
        limit = self.definition.max_chars
        if limit and len(content) > limit:
            issues.append(f"Content too long: {len(content)} chars (max {limit})")
        if not content.strip():
            issues.append("Content is empty")
        return not issues, issues

    def _media_urls(self, media: list[MediaAsset]) -> tuple[list[str], Optional[str]]:
        """Media goes along only where the platform takes that kind as itself or as an attachment. The rest is
        reported on the result, never silently dropped."""
        urls: list[str] = []
        skipped: set[str] = set()
        for asset in media:
            kind = asset.kind.value
            if self.definition.native_formats.get(kind) in ("native", "attachment"):
                urls.append(asset.url)
            else:
                skipped.add(kind)
        reason = None
        if skipped:
            reason = f"{self.definition.label} does not take {', '.join(sorted(skipped))} here, so it was left out."
        return urls, reason

    async def publish(self, request: PublishRequest, access_token: str = "") -> PublishResult:
        fields = self.config.get("fields") or {}
        if self.is_manual:
            return await ManualHandoffPublisher().publish(
                workspace_id=request.workspace_id,
                platform=request.platform,
                content=request.content,
                fields=fields,
                piece_id=request.piece_id,
            )

        media_urls, dropped = self._media_urls(request.media)
        result = await WebhookPublisher().publish(
            workspace_id=request.workspace_id,
            platform=request.platform,
            content=request.content,
            fields=fields,
            media_urls=media_urls,
            piece_id=request.piece_id,
            secrets=self.config.get("secrets") or {},
        )
        if dropped and result.success:
            result.media_dropped_reason = dropped
        return result
