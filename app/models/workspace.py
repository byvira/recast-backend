"""Pydantic models for workspaces, tiers, membership, and invites."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class WorkspaceTier(str, Enum):
    SINGLE = "single"
    DUO = "duo"
    LARGE = "large"


class WorkspaceRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class TierConfig(BaseModel):
    seats: int
    storage_limit_gb: int
    settings: dict = {}   # per-tier extras — expand later without migration


class CreateWorkspaceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    tier: WorkspaceTier


class Workspace(BaseModel):
    id: str
    name: str
    tier: WorkspaceTier
    tier_config: TierConfig      # snapshot at creation — never re-read from tiers.py after
    owner_id: str
    is_personal: bool = False    # auto-created at signup; non-deletable, single-seat
    created_at: datetime
    updated_at: datetime


class WorkspaceConnection(BaseModel):
    """A third-party platform account connected to a workspace.

    Replaces the per-user ``users.social_accounts[]`` array. Access and refresh
    tokens are stored encrypted alongside this document in MongoDB — never in
    this model, same convention as ``SocialAccount``.
    """

    id: str
    workspace_id: str
    platform: str                              # linkedin, instagram, threads, facebook, bluesky, google
    platform_user_id: str = ""
    username: str = ""
    is_active: bool = True
    connected_by: str = ""                     # user_id of the member who connected it (audit)
    connected_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_refreshed_at: Optional[datetime] = None


class WorkspaceMember(BaseModel):
    id: str
    workspace_id: str
    user_id: str
    role: WorkspaceRole
    status: str = "active"       # active | invited
    joined_at: datetime


class InviteStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"


class InviteMemberBody(BaseModel):
    email: str
    role: WorkspaceRole


class Invite(BaseModel):
    id: str
    workspace_id: str
    email: str
    role: WorkspaceRole
    token: str
    status: InviteStatus = InviteStatus.PENDING
    invited_by: str
    created_at: datetime
    expires_at: datetime