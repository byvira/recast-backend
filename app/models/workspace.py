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


class MediaUploadLimits(BaseModel):
    """Per-kind max upload size, in MB — app.api.v1.media reads these
    instead of a hardcoded constant, so a future pricing tier can vary
    them per workspace without a code change. Defaults match the original
    hardcoded values (app.api.v1.media.MAX_BYTES's old constants) — an
    existing workspace with this field unset behaves identically to
    before. Bounds are sanity limits, not a promise the provider (Cloudinary
    free tier) will actually accept a file that large — a real upload can
    still be rejected upstream; see the plan's note on verifying
    Cloudinary's actual per-file ceiling with a live test before relying
    on a value near the upper bound.
    """
    image_mb: int = Field(5, ge=1, le=25)
    video_mb: int = Field(50, ge=1, le=200)
    audio_mb: int = Field(20, ge=1, le=100)


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
    # Workspace-wide default language for generated content and Odette's
    # briefings — an opaque string, never validated against a fixed set.
    # None means "not configured"; callers fall through to the next
    # precedence level (see app.shared.language) rather than treating this
    # as "English". Settable via PATCH /api/v1/workspace/{id}.
    language: Optional[str] = None
    # None means "use the hardcoded defaults" — same precedence convention
    # as `language` above. Settable via PATCH /api/v1/workspace/{id}.
    media_upload_limits: Optional[MediaUploadLimits] = None
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
    # Best-effort, derived from platform + username/id at connect time —
    # not every platform's public profile URL is derivable from OAuth data
    # (e.g. LinkedIn, Google), so this is None for those.
    profile_url: Optional[str] = None
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