"""Support tickets — a workspace member files one, Recast staff resolve it
from the Ops Dashboard. Real persistence and a real staff-facing queue,
replacing the frontend's earlier local-only, lost-on-refresh mock.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SupportTicketSeverity(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class SupportTicketStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"


class SupportMessage(BaseModel):
    sender: str  # "member" or "staff"
    sender_name: str
    text: str
    created_at: datetime


class SupportTicket(BaseModel):
    id: str
    workspace_id: str
    workspace_name: str
    created_by: str
    created_by_name: str
    subject: str
    category: str
    severity: SupportTicketSeverity
    status: SupportTicketStatus
    messages: list[SupportMessage] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None


class SupportTicketCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    severity: SupportTicketSeverity = SupportTicketSeverity.P2
    description: str = Field(min_length=1, max_length=4000)


class SupportMessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SupportTicketStatusUpdate(BaseModel):
    status: SupportTicketStatus
