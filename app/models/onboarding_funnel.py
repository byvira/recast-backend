"""Pydantic models for onboarding funnel telemetry.

Minimal, purpose-built event log answering one question: which wizard step
do people actually stall at? Deliberately just two event types — enough to
reconstruct a real funnel (step_reached counts per step) and a finish line
(completed), without the complexity of a full analytics pipeline.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class FunnelEventType(str, Enum):
    STEP_REACHED = "step_reached"
    COMPLETED = "completed"


class LogFunnelEventBody(BaseModel):
    """Request body for POST /api/v1/onboarding/funnel-event."""

    event: FunnelEventType
    step: int
    step_title: str = ""
    total_steps: Optional[int] = None
    brand_type: Optional[str] = None
    brand_id: Optional[str] = None
