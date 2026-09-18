"""Pydantic models for campaign management.

Phase 1 of the "bulk campaigns" architecture — a campaign groups multiple
generation runs (today: one topic cluster -> N days of content via the
existing run_batch_pipeline()) under one tracked entity, so the Pipeline
page can show real, aggregate progress instead of the fully-mocked view
it had before. This model previously existed as a dead scaffold (no API
route or anything else in the codebase ever referenced it) — this is
the real thing.

Phase 1 deliberately only wires `content_types: ["text"]` — run_batch_
pipeline() is text-only and single-platform-set today. Audio/video/image
support and multi-platform-per-type campaigns are Phase 2 (would
generalise run_batch_pipeline itself, not just this model).
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ContentType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class CampaignCadence(BaseModel):
    """How generate-next-batch is meant to be run. Phase 1 stores this as
    plain metadata the caller acts on manually — recurring automation
    (a worker actually calling generate-next-batch on `frequency`) is
    Phase 3, not built here."""

    frequency: str = "manual"  # "manual" | "daily" | "weekly" — automation not built yet
    days_per_batch: int = 7  # passed straight to run_batch_pipeline's `days`


class Campaign(BaseModel):
    """A content campaign grouping multiple generation runs under one
    tracked entity, with real aggregate progress across its pieces."""

    id: str
    workspace_id: str
    brand_id: str
    name: str
    topic_cluster: str
    content_types: list[ContentType] = Field(default_factory=lambda: [ContentType.TEXT])
    platforms: list[str] = Field(default_factory=list)  # real Platform values, e.g. "LinkedIn"
    cadence: CampaignCadence = Field(default_factory=CampaignCadence)
    status: CampaignStatus = CampaignStatus.DRAFT
    # Every piece_id generated across every generate-next-batch run for
    # this campaign — content_pieces also carries campaign_id directly
    # (mirrors how session_id already links pieces), so this list is a
    # convenience/ordering record, not the source of truth for membership.
    piece_ids: list[str] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    updated_at: datetime


class CreateCampaignRequest(BaseModel):
    name: str
    brand_id: str
    topic_cluster: str
    platforms: list[str]
    cadence: CampaignCadence = Field(default_factory=CampaignCadence)


class UpdateCampaignRequest(BaseModel):
    name: Optional[str] = None
    topic_cluster: Optional[str] = None
    platforms: Optional[list[str]] = None
    status: Optional[CampaignStatus] = None
    cadence: Optional[CampaignCadence] = None


class CampaignSchedule(BaseModel):
    """Scheduling configuration for a campaign's recurring publish events —
    Phase 3 (cadence automation). Not wired to anything yet; kept here so
    the eventual worker has a settled shape to write against."""

    campaign_id: str
    platform: str
    publish_at: datetime
    recurring: bool = False
    recurrence_rule: Optional[str] = None
