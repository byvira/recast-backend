"""Background runs: list them, read one, pause, resume or cancel it. Starting one is done by the feature that owns the work
(for example `POST /campaigns/{id}/runs`)."""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.shared import pipeline_runs

router = APIRouter()


@router.get("")
@limiter.limit("120/minute")
async def list_runs(
    request: Request,
    kind: Optional[Literal["campaign", "text", "audio", "image", "video"]] = None,
    campaign_id: Optional[str] = None,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """Active runs and the ones that finished in the last two days, newest first."""
    ref = {"campaign_id": campaign_id} if campaign_id else None
    rows = await pipeline_runs.list_runs(ctx.workspace_id, kind=kind, ref=ref)
    return {"runs": [pipeline_runs.public(r) for r in rows]}


@router.get("/{run_id}")
@limiter.limit("240/minute")
async def get_run(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    doc = await pipeline_runs.get_run(run_id, ctx.workspace_id)
    if not doc:
        raise HTTPException(status_code=404, detail="That run was not found.")
    return pipeline_runs.public(doc)


@router.post("/{run_id}/{action}")
@limiter.limit("60/minute")
async def control_run(
    request: Request,
    run_id: str,
    action: Literal["pause", "resume", "cancel"],
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    doc = await pipeline_runs.control(run_id, ctx.workspace_id, action)
    if not doc:
        raise HTTPException(status_code=404, detail="That run was not found.")
    return pipeline_runs.public(doc)
