"""Pydantic models for campaign management."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Campaign(BaseModel):
    """A content campaign grouping multiple pipeline jobs."""

    id: str
    name: str
    brand_id: str
    pipelines: list[str] = []
    status: str = "draft"
    description: Optional[str] = None
    created_at: datetime


class CampaignSchedule(BaseModel):
    """Scheduling configuration for a campaign's publish events."""

    campaign_id: str
    platform: str
    publish_at: datetime
    recurring: bool = False
    recurrence_rule: Optional[str] = None
