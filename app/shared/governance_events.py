"""Thin fire-and-forget emitters for workspace-governance events.

These feed the Layer-2 supervisor's rule engine and digest. Every helper is
non-blocking (``emit_event_background``) and never raises into the caller, so
wiring them into a route adds no latency and no failure mode.

``pipeline_type`` is always ``None`` for these — they are workspace-wide, not
tied to any content pipeline (the event envelope enforces that).
"""

from __future__ import annotations

from typing import Optional

from app.models.agent_events import (
    BrandVoiceUpdatedPayload,
    ContentPublishedPayload,
    EventType,
    MemberChangePayload,
    RoleChangedPayload,
    TierChangedPayload,
)
from app.shared.events import emit_event_background
from app.shared.pipeline_types import PipelineType


def emit_content_published(workspace_id: str, *, pipeline_type: "PipelineType | str",
                           actor_user_id: str, actor_role: str, content_id: str,
                           target: str = "", external_url: str = "") -> None:
    """A content piece went live on a platform. Feeds the daily_publish_cap rule.
    ``pipeline_type`` is the content's pipeline (required for content.* events)."""
    emit_event_background(
        event_type=EventType.CONTENT_PUBLISHED, pipeline_type=pipeline_type,
        workspace_id=workspace_id, actor_user_id=actor_user_id, actor_role=actor_role,
        payload=ContentPublishedPayload(content_id=content_id, target=target,
                                        external_url=external_url),
        idempotency_key=f"content.published:{content_id}:{target}",
    )


def emit_member_added(workspace_id: str, *, actor_user_id: str, actor_role: str,
                      subject_user_id: str, role: str) -> None:
    emit_event_background(
        event_type=EventType.MEMBER_ADDED, pipeline_type=None,
        workspace_id=workspace_id, actor_user_id=actor_user_id, actor_role=actor_role,
        payload=MemberChangePayload(subject_user_id=subject_user_id, role=role),
        idempotency_key=f"member.added:{workspace_id}:{subject_user_id}",
    )


def emit_member_removed(workspace_id: str, *, actor_user_id: str, actor_role: str,
                        subject_user_id: str, role: str = "") -> None:
    emit_event_background(
        event_type=EventType.MEMBER_REMOVED, pipeline_type=None,
        workspace_id=workspace_id, actor_user_id=actor_user_id, actor_role=actor_role,
        payload=MemberChangePayload(subject_user_id=subject_user_id, role=role),
        idempotency_key=f"member.removed:{workspace_id}:{subject_user_id}",
    )


def emit_role_changed(workspace_id: str, *, actor_user_id: str, actor_role: str,
                      subject_user_id: str, from_role: str, to_role: str) -> None:
    emit_event_background(
        event_type=EventType.ROLE_CHANGED, pipeline_type=None,
        workspace_id=workspace_id, actor_user_id=actor_user_id, actor_role=actor_role,
        payload=RoleChangedPayload(subject_user_id=subject_user_id,
                                   from_role=from_role, to_role=to_role),
        idempotency_key=f"role.changed:{workspace_id}:{subject_user_id}:{to_role}",
    )


def emit_brand_voice_updated(workspace_id: str, *, actor_user_id: str, actor_role: str,
                             brand_id: str, changed_fields: Optional[list[str]] = None,
                             diff_summary: str = "") -> None:
    emit_event_background(
        event_type=EventType.BRAND_VOICE_UPDATED, pipeline_type=None,
        workspace_id=workspace_id, actor_user_id=actor_user_id, actor_role=actor_role,
        payload=BrandVoiceUpdatedPayload(brand_id=brand_id,
                                         changed_fields=changed_fields or [],
                                         diff_summary=diff_summary),
        # no fixed idempotency key — every edit is its own event
    )


def emit_tier_changed(workspace_id: str, *, actor_user_id: str, actor_role: str,
                      from_tier: str, to_tier: str, seats_from: int = 0, seats_to: int = 0) -> None:
    emit_event_background(
        event_type=EventType.TIER_CHANGED, pipeline_type=None,
        workspace_id=workspace_id, actor_user_id=actor_user_id, actor_role=actor_role,
        payload=TierChangedPayload(from_tier=from_tier, to_tier=to_tier,
                                   seats_from=seats_from, seats_to=seats_to),
    )
