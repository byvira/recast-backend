"""Workspace request-scoping — the ``get_current_workspace`` dependency and helpers.

Every per-account route resolves its workspace here instead of reading ``user_id``
off the token. Resolution order:

1. ``X-Workspace-Id`` request header, if present
2. ``user.default_workspace_id`` (set to the personal workspace at signup)
3. otherwise → 400, the caller has no workspace context

The resolved workspace membership row carries the caller's ``role``, so RBAC
checks (:func:`app.core.rbac.assert_permission`) run with no extra query.
"""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import structlog
from fastapi import Depends, Header, HTTPException

from app.core.auth import get_current_user
from app.core.rbac import assert_permission
from app.core.tiers import TIER_DEFAULTS
from app.db.mongo import workspace_members, workspaces
from app.models.workspace import WorkspaceRole, WorkspaceTier


class WorkspaceContext:
    """Resolved per-request workspace scope.

    Attributes:
        workspace: The ``workspaces`` document.
        member:    The caller's ``workspace_members`` document (has ``role``).
        user:      The authenticated ``users`` document.
    """

    __slots__ = ("workspace", "member", "user")

    def __init__(self, workspace: dict[str, Any], member: dict[str, Any], user: dict[str, Any]) -> None:
        self.workspace = workspace
        self.member = member
        self.user = user

    @property
    def workspace_id(self) -> str:
        return self.workspace["id"]

    @property
    def user_id(self) -> str:
        return self.user["id"]

    @property
    def role(self) -> str:
        return self.member["role"]


async def get_current_workspace(
    current_user: dict[str, Any] = Depends(get_current_user),
    x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id"),
) -> WorkspaceContext:
    """Resolve and authorise the caller's active workspace for this request.

    Raises:
        HTTPException 400: No ``X-Workspace-Id`` header and no ``default_workspace_id``.
        HTTPException 403: Caller is not an active member of the target workspace.
        HTTPException 404: Membership exists but the workspace document is gone.
    """
    workspace_id = x_workspace_id or current_user.get("default_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400,
            detail="No workspace context. Send an X-Workspace-Id header or set a default workspace.",
        )

    member = await workspace_members.find_one(
        {"workspace_id": workspace_id, "user_id": current_user["id"]}
    )
    if not member or member.get("status") != "active":
        raise HTTPException(status_code=403, detail="Not a member of this workspace.")

    workspace = await workspaces.find_one({"id": workspace_id})
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    structlog.contextvars.bind_contextvars(workspace_id=workspace_id)
    return WorkspaceContext(workspace=workspace, member=member, user=current_user)


def require(permission: str):
    """Dependency factory — resolve the workspace, then assert a permission on it.

    Usage::

        @router.post("/")
        async def handler(ctx: WorkspaceContext = Depends(require("edit_brand_voice"))):
            ...
    """

    async def _dep(ctx: WorkspaceContext = Depends(get_current_workspace)) -> WorkspaceContext:
        assert_permission(ctx.member, permission)
        return ctx

    return _dep


async def create_personal_workspace(user_id: str, name: str) -> str:
    """Create a user's personal workspace + owner membership. Returns the workspace id.

    Called from signup. Personal workspaces are ``is_personal=True`` (single seat,
    non-deletable) and snapshot the ``single`` tier config.
    """
    now = datetime.now(timezone.utc)
    workspace_id = str(uuid4())
    tier_config = TIER_DEFAULTS[WorkspaceTier.SINGLE]

    await workspaces.insert_one(
        {
            "id": workspace_id,
            "name": name,
            "tier": WorkspaceTier.SINGLE.value,
            "tier_config": tier_config.model_dump(),
            "owner_id": user_id,
            "is_personal": True,
            "created_at": now,
            "updated_at": now,
        }
    )
    await workspace_members.insert_one(
        {
            "id": str(uuid4()),
            "workspace_id": workspace_id,
            "user_id": user_id,
            "role": WorkspaceRole.OWNER.value,
            "status": "active",
            "joined_at": now,
        }
    )
    return workspace_id
