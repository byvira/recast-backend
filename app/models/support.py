"""Support tickets — a workspace member files one, Recast staff resolve it
from the Ops Dashboard. Real persistence and a real staff-facing queue,
replacing the frontend's earlier local-only, lost-on-refresh mock.

Two views of the same document: ``SupportTicket`` is the staff view (internal
notes, assignee, tags); ``SupportTicketMemberView`` is what a member may ever
see. Member routes must serialize through ``app.shared.support.member_view``,
never return the stored document directly, so an internal note cannot leak.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.shared.support_rules import MAX_ATTACHMENTS_PER_MESSAGE, MAX_MESSAGE_CHARS


class SupportTicketSeverity(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class SupportTicketStatus(str, Enum):
    OPEN = "open"
    # Stored value kept as "investigating" so tickets filed before the
    # expanded lifecycle need no migration; the UI labels it "In progress".
    INVESTIGATING = "investigating"
    WAITING_ON_MEMBER = "waiting_on_member"
    WAITING_ON_ENGINEERING = "waiting_on_engineering"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SupportStaffRole(str, Enum):
    AGENT = "agent"
    LEAD = "lead"
    ADMIN = "admin"


class SupportAttachment(BaseModel):
    file_id: str
    name: str
    mime: str
    size: int


class SupportMessage(BaseModel):
    sender: str  # "member", "staff" or "system"
    sender_name: str
    text: str
    created_at: datetime
    # Staff-only note. Never leaves an ops route.
    is_internal: bool = False
    attachments: list[SupportAttachment] = Field(default_factory=list)
    # A message an admin removed. The words are gone; only the fact remains.
    is_deleted: bool = False


class SupportTicket(BaseModel):
    """Staff view of a ticket."""

    id: str
    number: Optional[int] = None
    workspace_id: str
    workspace_name: str
    created_by: str
    created_by_name: str
    subject: str
    category: str
    severity: SupportTicketSeverity
    status: SupportTicketStatus
    messages: list[SupportMessage] = Field(default_factory=list)
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    related_ticket_id: Optional[str] = None
    source_context: Optional[dict] = None
    client_env: Optional[dict] = None
    closed_reason: Optional[str] = None
    created_by_email: Optional[str] = None
    workspace_tier: Optional[str] = None
    snoozed_until: Optional[datetime] = None
    suggested_category: Optional[str] = None
    rating: Optional[dict] = None
    # Set by the staff routes when the member or workspace no longer exists.
    created_by_deleted: bool = False
    workspace_deleted: bool = False
    sla: Optional[dict] = None
    merged_into: Optional[str] = None
    incident_id: Optional[str] = None
    unread_for_member: bool = False
    unread_for_ops: bool = True
    last_member_message_at: Optional[datetime] = None
    last_staff_message_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class SupportTicketMemberView(BaseModel):
    """Everything a member is allowed to see of their own ticket."""

    id: str
    number: Optional[int] = None
    workspace_name: str
    subject: str
    category: str
    severity: SupportTicketSeverity
    status: SupportTicketStatus
    messages: list[SupportMessage] = Field(default_factory=list)
    assignee_name: Optional[str] = None
    # False when a workspace admin is reading a teammate's ticket (read-only).
    is_own: bool = True
    created_by_name: Optional[str] = None
    closed_reason: Optional[str] = None
    unread_for_member: bool = False
    # Long threads come in pages: the newest ones first, older on request.
    message_total: Optional[int] = None
    has_older: bool = False
    first_index: int = 0
    rating: Optional[dict] = None
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class SupportRatingBody(BaseModel):
    value: Literal["up", "down"]
    comment: Optional[str] = Field(default=None, max_length=1000)


class SupportTicketEvent(BaseModel):
    """One row of the append-only audit trail."""

    id: str
    ticket_id: str
    actor_type: str  # "member" | "staff" | "system"
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    type: str
    data: dict = Field(default_factory=dict)
    created_at: datetime


class SupportSourceContext(BaseModel):
    """What the member was looking at when they filed the ticket."""

    type: str = Field(pattern="^(post|platform|generation|page)$")
    id: Optional[str] = Field(default=None, max_length=80)
    route: Optional[str] = Field(default=None, max_length=200)


class SupportClientEnv(BaseModel):
    user_agent: Optional[str] = Field(default=None, max_length=300)
    viewport: Optional[str] = Field(default=None, max_length=30)


class SupportTicketCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    severity: SupportTicketSeverity = SupportTicketSeverity.P2
    description: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE)
    # Set when the member chose to file anyway after the duplicate prompt.
    allow_duplicate: bool = False
    source_context: Optional[SupportSourceContext] = None
    client_env: Optional[SupportClientEnv] = None
    related_ticket_id: Optional[str] = Field(default=None, max_length=80)


class SupportMessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE)


class SupportStaffMessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_MESSAGE)
    is_internal: bool = False
    # "Send and set status" — applied through the same transition table.
    set_status: Optional[SupportTicketStatus] = None
    # The number of messages the sender had on screen. If more have arrived
    # since, the reply is held back until they confirm with `force`.
    known_message_total: Optional[int] = Field(default=None, ge=0)
    force: bool = False


class SupportTicketStatusUpdate(BaseModel):
    status: SupportTicketStatus


class SupportTicketStaffUpdate(BaseModel):
    """Partial staff edit. Only the fields sent are changed."""

    status: Optional[SupportTicketStatus] = None
    severity: Optional[SupportTicketSeverity] = None
    # "" unassigns; a user id assigns (lead or admin to assign someone else).
    assignee_id: Optional[str] = None
    tags: Optional[list[str]] = Field(default=None, max_length=12)
    # A time in the future hides the ticket from the default queue until then;
    # sending null through `clear_snooze` brings it back now.
    snoozed_until: Optional[datetime] = None
    clear_snooze: bool = False


class SupportTicketCloseRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)
