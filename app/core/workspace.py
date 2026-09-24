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
from fastapi import Depends, Header, HTTPException, Query

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
    return await _resolve_workspace(current_user, x_workspace_id)


async def get_stream_workspace(
    current_user: dict[str, Any] = Depends(get_current_user),
    x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id"),
    workspace_id: Optional[str] = Query(default=None, max_length=64),
) -> WorkspaceContext:
    """``get_current_workspace`` for Server-Sent Events routes.

    A browser ``EventSource`` can't set request headers, so without this an
    SSE route always fell back to the caller's *default* workspace no matter
    which one they'd switched to. Same resolution and membership check —
    the workspace can additionally come from a ``workspace_id`` query param.
    """
    return await _resolve_workspace(current_user, x_workspace_id or workspace_id)


async def _resolve_workspace(current_user: dict[str, Any], requested: Optional[str]) -> WorkspaceContext:
    workspace_id = requested or current_user.get("default_workspace_id")
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


def require_ops_admin(permission: str):
    """Ops Dashboard variant of require() — a master admin
    (user.is_master_admin) gets *permission* on ANY workspace, without
    needing real membership in it. Every other caller falls through to the
    exact real check require() does (real membership row, real role, real
    assert_permission) — a master admin is a strictly additive grant, never
    a shortcut that changes behavior for anyone else.

    Deliberately confined to the three Ops Dashboard route files
    (ops_platforms.py, ops_ai_budget.py, ops_cohorts.py), each of which
    opts in explicitly by using this instead of require(). Never make this
    the default for require() itself — a master admin must gain access to
    the ops/platform-config surface, not silently to every brand/content/
    campaign endpoint in the product, which is a far bigger grant than
    "the ops dashboard" asks for.

    Usage identical to require()::

        _OWNER = require_ops_admin("manage_workspace_settings")
    """

    async def _dep(
        current_user: dict[str, Any] = Depends(get_current_user),
        x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id"),
    ) -> WorkspaceContext:
        if not current_user.get("is_master_admin"):
            ctx = await _resolve_workspace(current_user, x_workspace_id)
            assert_permission(ctx.member, permission)
            return ctx

        workspace_id = x_workspace_id or current_user.get("default_workspace_id")
        if not workspace_id:
            raise HTTPException(
                status_code=400,
                detail="No workspace context. Send an X-Workspace-Id header or set a default workspace.",
            )
        workspace = await workspaces.find_one({"id": workspace_id})
        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found.")

        # Synthetic owner membership — never written to workspace_members,
        # exists only for this request so downstream code that reads
        # ctx.member/ctx.role (audit trails, created_by fields) still gets
        # a sensible value instead of a real (possibly nonexistent) row.
        synthetic_member: dict[str, Any] = {
            "id": "master-admin",
            "workspace_id": workspace_id,
            "user_id": current_user["id"],
            "role": WorkspaceRole.OWNER.value,
            "status": "active",
        }
        structlog.contextvars.bind_contextvars(workspace_id=workspace_id, master_admin=True)
        return WorkspaceContext(workspace=workspace, member=synthetic_member, user=current_user)

    return _dep


def require_stream(permission: str):
    """``require`` for SSE routes — see ``get_stream_workspace``."""

    async def _dep(ctx: WorkspaceContext = Depends(get_stream_workspace)) -> WorkspaceContext:
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
            "language": None,
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
