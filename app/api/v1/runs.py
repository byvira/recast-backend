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
@limiter.limit("300/minute")
async def list_runs(
    request: Request,
    kind: Optional[Literal["campaign", "text", "audio", "image", "video", "odette"]] = None,
    campaign_id: Optional[str] = None,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """Active runs and the ones that finished in the last two days, newest first."""
    ref = {"campaign_id": campaign_id} if campaign_id else None
    rows = await pipeline_runs.list_runs(ctx.workspace_id, kind=kind, ref=ref)
    return {"runs": [pipeline_runs.public(r) for r in rows]}


# A progress check is a cheap read of one saved row, and a screen asks for it every few seconds for as long as the work runs, so this
# limit is far above what one person can reach and only stops a script.
@router.get("/{run_id}")
@limiter.limit("600/minute")
async def get_run(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    doc = await pipeline_runs.get_run(run_id, ctx.workspace_id)
    if not doc:
        raise HTTPException(status_code=404, detail="That run was not found.")
    return pipeline_runs.public(doc)


@router.post("/{run_id}/retry", status_code=202)
@limiter.limit("20/minute")
async def retry_run(request: Request, run_id: str, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Starts a failed run again, as a new run with the same settings. Only work that is safe to repeat (it does the same thing when run
    again, so it cannot double up) can be tried again; everything else says so and the member starts it again from where it began. One
    failed run is tried again once."""
    from app.shared import jobs

    existing = await pipeline_runs.get_run(run_id, ctx.workspace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="That run was not found.")
    if existing.get("created_by") != ctx.user_id and ctx.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Only the person who started this, or an admin, can try it again.")
    if existing.get("status") != "failed":
        raise HTTPException(status_code=409, detail="Only work that failed can be tried again.")
    if not (existing.get("restartable") and existing.get("action") and existing.get("payload")):
        raise HTTPException(status_code=409, detail="This one can't be repeated for you. Open it and start it again.")
    # Claimed with one write, so two quick taps (or two people) make one new run.
    claimed = await pipeline_runs.pipeline_runs.find_one_and_update(
        {"id": run_id, "workspace_id": ctx.workspace_id, "status": "failed", "retry_run_id": {"$exists": False}},
        {"$set": {"retry_run_id": "starting"}},
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="This was already tried again.")
    try:
        run, _ = await jobs.submit(action_name=existing["action"], payload=existing["payload"], ctx=ctx)
    except BaseException:
        await pipeline_runs.pipeline_runs.update_one({"id": run_id, "workspace_id": ctx.workspace_id}, {"$unset": {"retry_run_id": ""}})
        raise
    await pipeline_runs.pipeline_runs.update_one({"id": run_id, "workspace_id": ctx.workspace_id}, {"$set": {"retry_run_id": run["id"]}})
    return run


@router.post("/{run_id}/{action}")
@limiter.limit("60/minute")
async def control_run(
    request: Request,
    run_id: str,
    action: Literal["pause", "resume", "cancel"],
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    existing = await pipeline_runs.get_run(run_id, ctx.workspace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="That run was not found.")
    # Whoever started the work, or an owner or admin, can pause, resume or cancel it; another member cannot stop someone's publish.
    if existing.get("created_by") != ctx.user_id and ctx.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Only the person who started this, or an admin, can pause or cancel it.")
    doc = await pipeline_runs.control(run_id, ctx.workspace_id, action)
    if not doc:
        raise HTTPException(status_code=404, detail="That run was not found.")
    return pipeline_runs.public(doc)
