"""Pydantic models for workspaces, tiers, membership, and invites."""

from datetime import datetime
from enum import Enum
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
    created_at: datetime
    updated_at: datetime


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