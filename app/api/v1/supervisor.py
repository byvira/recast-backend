"""Workspace-supervisor API — admin/owner only ("Odette").

Every route is gated by ``require("view_workspace_insights")`` — a permission
held only by the ``owner`` and ``admin`` roles (see ``app/core/rbac.py``). This
is the same permission-set pattern every other admin-gated route uses; no new
access model. ``/supervisor/run`` additionally requires ``manage_workspace_settings``
(owner only).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.agents.supervisor import service
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require

router = APIRouter()
logger = logging.getLogger(__name__)

_ADMIN = require("view_workspace_insights")
_OWNER = require("manage_workspace_settings")


class StatusBody(BaseModel):
    status: str


@router.get("/insights")
@limiter.limit("30/minute")
async def get_insights(
    request: Request,
    status: Optional[str] = Query(None, pattern="^(new|seen|dismissed|actioned)$"),
    limit: int = Query(50, ge=1, le=200),
    ctx: WorkspaceContext = Depends(_ADMIN),
) -> dict:
    return await service.list_insights(ctx.workspace_id, status=status, limit=limit)


@router.post("/insights/{insight_id}/status")
@limiter.limit("60/minute")
async def update_insight_status(
    request: Request,
    insight_id: str,
    body: StatusBody,
    ctx: WorkspaceContext = Depends(_ADMIN),
) -> dict:
    return await service.set_insight_status(ctx.workspace_id, insight_id, body.status)


@router.get("/flags")
@limiter.limit("30/minute")
async def get_flags(
    request: Request,
    status: Optional[str] = Query("open", pattern="^(open|resolved|muted)$"),
    limit: int = Query(50, ge=1, le=200),
    ctx: WorkspaceContext = Depends(_ADMIN),
) -> dict:
    return await service.list_flags(ctx.workspace_id, status=status, limit=limit)


@router.post("/flags/{flag_id}/status")
@limiter.limit("60/minute")
async def update_flag_status(
    request: Request,
    flag_id: str,
    body: StatusBody,
    ctx: WorkspaceContext = Depends(_ADMIN),
) -> dict:
    return await service.set_flag_status(ctx.workspace_id, flag_id, body.status)


@router.get("/dashboard")
@limiter.limit("30/minute")
async def get_dashboard(
    request: Request,
    ctx: WorkspaceContext = Depends(_ADMIN),
) -> dict:
    return await service.dashboard(ctx.workspace_id)


@router.get("/notifications")
@limiter.limit("60/minute")
async def get_notifications(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    ctx: WorkspaceContext = Depends(_ADMIN),
) -> dict:
    return await service.list_notifications(ctx.workspace_id, ctx.user_id, limit=limit)


@router.post("/notifications/{notif_id}/read")
@limiter.limit("120/minute")
async def read_notification(
    request: Request,
    notif_id: str,
    ctx: WorkspaceContext = Depends(_ADMIN),
) -> dict:
    return await service.mark_notification_read(ctx.workspace_id, ctx.user_id, notif_id)


@router.post("/run")
@limiter.limit("5/minute")
async def run_supervisor_pass(
    request: Request,
    ctx: WorkspaceContext = Depends(_OWNER),
) -> dict:
    """Owner-only: trigger a supervisor reasoning pass now (enqueues on the arq
    worker). Returns immediately."""
    return await service.trigger_run(ctx.workspace_id)
