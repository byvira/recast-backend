"""Event schema for the two-layer agent architecture.

Pipeline-agnostic by construction:

* ``pipeline_type`` is a **required** field on the envelope — it has no default.
  A producer that forgets it gets a ``ValidationError``; it can never silently
  fall back to ``"text"``.
* A model validator enforces the rule that every ``content.*`` / ``pipeline.*``
  event names its pipeline, while non-pipeline events (member / role / tier /
  brand) leave it ``null``.
* Every payload family carries medium-neutral keys (``content_text`` holds a
  transcript / caption / OCR dump just as well as body text), so Audio, Image
  and Video emit the *same* envelope with zero schema changes.

Two payload families:

A. Workspace events — ``content.*``, ``pipeline.*``, ``member.*``, ``role.*``,
   ``brand.*``, ``tier.*``. Emitted by API routes / storage / workers.
B. Assistant signals — ``assistant.signal``. Emitted only by a Layer-1 personal
   assistant; ``actor_user_id`` is the member the signal is *about*.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.shared.pipeline_types import PipelineType


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class EventType(str, Enum):
    # Family A — content / pipeline (REQUIRE a non-null pipeline_type)
    CONTENT_CREATED = "content.created"
    CONTENT_UPDATED = "content.updated"
    CONTENT_PUBLISHED = "content.published"
    PIPELINE_RUN_COMPLETED = "pipeline.run_completed"
    # Family A — workspace governance (REQUIRE a null pipeline_type)
    MEMBER_ADDED = "member.added"
    MEMBER_REMOVED = "member.removed"
    ROLE_CHANGED = "role.changed"
    BRAND_VOICE_UPDATED = "brand.voice_updated"
    TIER_CHANGED = "tier.changed"
    # Family B — emitted by the personal assistant (pipeline_type optional)
    ASSISTANT_SIGNAL = "assistant.signal"


#: Events that MUST carry a concrete ``pipeline_type``.
PIPELINE_SCOPED_EVENTS: frozenset[EventType] = frozenset({
    EventType.CONTENT_CREATED,
    EventType.CONTENT_UPDATED,
    EventType.CONTENT_PUBLISHED,
    EventType.PIPELINE_RUN_COMPLETED,
})

#: Events that MUST NOT carry a ``pipeline_type`` (they are workspace-wide).
NON_PIPELINE_EVENTS: frozenset[EventType] = frozenset({
    EventType.MEMBER_ADDED,
    EventType.MEMBER_REMOVED,
    EventType.ROLE_CHANGED,
    EventType.BRAND_VOICE_UPDATED,
    EventType.TIER_CHANGED,
})
# ``ASSISTANT_SIGNAL`` is in neither set: pipeline_type is the pipeline of the
# observed content, or null for a cross-pipeline signal (e.g. output volume).


class SignalType(str, Enum):
    VOICE_DRIFT = "voice_drift"
    VOICE_DRIFT_TREND = "voice_drift_trend"
    VOLUME_SPIKE = "volume_spike"
    VOLUME_DROP = "volume_drop"
    TOPIC_SHIFT = "topic_shift"
    QUALITY_REGRESSION = "quality_regression"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ─────────────────────────────────────────────────────────────────────────────
# Shared value objects
# ─────────────────────────────────────────────────────────────────────────────

class ContentRef(BaseModel):
    """A generic pointer to a stored document — never assumed to be
    ``content_pieces``. Audio/Image/Video point at their own collections."""

    collection: str
    id: str


# ─────────────────────────────────────────────────────────────────────────────
# Payload family A — workspace events
# ─────────────────────────────────────────────────────────────────────────────

class ContentEventPayload(BaseModel):
    """Payload for ``content.created`` and ``content.updated``.

    ``content_text`` is medium-neutral: full body for text, transcript for
    audio/video, caption + OCR + alt-text for image. ``target`` is an opaque
    route label ("LinkedIn", "Podcast", "YouTube") — never parsed by an agent.
    """

    content_id: str
    content_ref: ContentRef
    content_text: str = ""
    content_summary: str = ""
    target: str = ""
    word_count: int = 0
    quality_passed: bool = True
    flagged_for_review: bool = False
    brand_id: str = ""
    session_id: str = ""


class ContentPublishedPayload(BaseModel):
    content_id: str
    target: str = ""
    external_url: str = ""
    published_at: Optional[datetime] = None


class PipelineRunCompletedPayload(BaseModel):
    session_id: str
    pieces: int = 0
    failed: int = 0
    duration_ms: int = 0
    brand_id: str = ""


class MemberChangePayload(BaseModel):
    """``member.added`` / ``member.removed``."""

    subject_user_id: str
    role: str = ""


class RoleChangedPayload(BaseModel):
    subject_user_id: str
    from_role: str = ""
    to_role: str = ""


class BrandVoiceUpdatedPayload(BaseModel):
    brand_id: str
    changed_fields: list[str] = Field(default_factory=list)
    diff_summary: str = ""


class TierChangedPayload(BaseModel):
    from_tier: str = ""
    to_tier: str = ""
    seats_from: int = 0
    seats_to: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Payload family B — assistant signal
# ─────────────────────────────────────────────────────────────────────────────

class SignalMetric(BaseModel):
    name: str
    value: float
    baseline: float = 0.0
    threshold: float = 0.0


class SignalWindow(BaseModel):
    kind: str = "rolling"      # "rolling" | "trend"
    n: int = 0


class AssistantSignalPayload(BaseModel):
    signal_type: SignalType
    severity: Severity
    metric: SignalMetric
    window: SignalWindow = Field(default_factory=SignalWindow)
    evidence_refs: list[ContentRef] = Field(default_factory=list)
    member_message: str = ""       # Remy's voice — shown to the member only
    supervisor_note: str = ""      # terse, factual — for Odette's digest
    persona: str = "Remy"
    emitted_by: str = "personal_assistant"


# ─────────────────────────────────────────────────────────────────────────────
# Envelope
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkspaceEvent(BaseModel):
    """The durable event envelope. Persisted to ``workspace_events`` and mirrored
    onto the ``recast:events`` Redis Stream by :func:`app.shared.events.emit_event`.
    """

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: EventType

    # REQUIRED, no default. ``Field(...)`` = the caller must pass it explicitly,
    # even when the correct value is ``None`` (non-pipeline events). This is the
    # structural guarantee that nothing silently defaults to "text".
    pipeline_type: Optional[PipelineType] = Field(...)

    workspace_id: str
    actor_user_id: str
    actor_role: str = ""

    occurred_at: datetime = Field(default_factory=_utcnow)
    ingested_at: Optional[datetime] = None      # stamped by emit_event
    schema_version: int = 1

    # Producer-supplied; a unique index on this dedupes at-least-once redelivery.
    idempotency_key: str

    payload: dict[str, Any] = Field(default_factory=dict)
    consumed_by: dict[str, bool] = Field(
        default_factory=lambda: {"personal": False, "supervisor": False}
    )

    @model_validator(mode="after")
    def _enforce_pipeline_type_rule(self) -> "WorkspaceEvent":
        et = self.event_type
        if et in PIPELINE_SCOPED_EVENTS and self.pipeline_type is None:
            raise ValueError(
                f"event_type '{et.value}' requires a concrete pipeline_type "
                f"(text|audio|image|video); got None"
            )
        if et in NON_PIPELINE_EVENTS and self.pipeline_type is not None:
            raise ValueError(
                f"event_type '{et.value}' is workspace-wide and must have "
                f"pipeline_type=None; got '{self.pipeline_type}'"
            )
        return self


#: Maps an event type to the payload model that validates its ``payload`` dict.
#: Used by producers that want a typed build; :func:`emit_event` also runs the
#: matching model in strict mode so a malformed payload fails closed.
PAYLOAD_MODELS: dict[EventType, type[BaseModel]] = {
    EventType.CONTENT_CREATED: ContentEventPayload,
    EventType.CONTENT_UPDATED: ContentEventPayload,
    EventType.CONTENT_PUBLISHED: ContentPublishedPayload,
    EventType.PIPELINE_RUN_COMPLETED: PipelineRunCompletedPayload,
    EventType.MEMBER_ADDED: MemberChangePayload,
    EventType.MEMBER_REMOVED: MemberChangePayload,
    EventType.ROLE_CHANGED: RoleChangedPayload,
    EventType.BRAND_VOICE_UPDATED: BrandVoiceUpdatedPayload,
    EventType.TIER_CHANGED: TierChangedPayload,
    EventType.ASSISTANT_SIGNAL: AssistantSignalPayload,
}
