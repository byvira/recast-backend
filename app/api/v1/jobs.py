"""One route to start any registered action as a background job (see `app.shared.jobs`). Progress, pause, resume and cancel are
on `/api/v1/runs/{id}`."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.shared import jobs

router = APIRouter()


class StartJob(BaseModel):
    action: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)


@router.get("/actions")
@limiter.limit("60/minute")
async def list_actions(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """Every action that can be started as a job, with the permission it needs."""
    return {"actions": jobs.action_summaries()}


@router.post("", status_code=202)
@limiter.limit("30/minute")
async def start_job(
    request: Request,
    body: StartJob,
    response: Response,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Start the action in the background and answer at once with its run. The same action with the same payload, while one is
    still going, answers with that run (`already_running`) instead of starting another."""
    run, already = await jobs.submit(action_name=body.action, payload=body.payload, ctx=ctx, client_key=idempotency_key)
    if already:
        response.status_code = 200
    return {**run, "already_running": already}
