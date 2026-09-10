"""Personal-assistant API — member-scoped ("Remy").

Every route resolves the caller's active workspace and then hard-scopes to
``ctx.user_id``. A persona and its signals are private to the member they belong
to; no other member — and no workspace admin — can read them here.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.agents.personal import service
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require

router = APIRouter()
logger = logging.getLogger(__name__)


class AssistRequest(BaseModel):
    draft_text: str = Field(..., min_length=1)
    pipeline_type: str = "text"      # which pipeline the draft belongs to
    target: str = ""                 # platform / route label, opaque
    brand_id: Optional[str] = None   # accepted for forward-compat; not required today


@router.get("/persona")
@limiter.limit("30/minute")
async def get_my_persona(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The caller's own voice persona — style fingerprint, topics, volume,
    drift history. 404 until Remy has seen some of their content."""
    return await service.get_persona(ctx.workspace_id, ctx.user_id)


@router.get("/signals")
@limiter.limit("60/minute")
async def get_my_signals(
    request: Request,
    status: Optional[str] = Query(None, pattern="^(open|acknowledged|auto_resolved)$"),
    limit: int = Query(50, ge=1, le=200),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The caller's own assistant signals (voice drift, volume, topic, quality)."""
    return await service.list_signals(ctx.workspace_id, ctx.user_id, status=status, limit=limit)


@router.post("/signals/{signal_id}/acknowledge")
@limiter.limit("60/minute")
async def acknowledge_my_signal(
    request: Request,
    signal_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    return await service.acknowledge_signal(ctx.workspace_id, ctx.user_id, signal_id)


@router.post("/assist")
@limiter.limit("20/minute")
async def assist_with_draft(
    request: Request,
    body: AssistRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict:
    """Given a work-in-progress draft, tell the member whether it matches their
    established voice and how to pull it back if not. User-initiated — makes one
    Groq call. Never on any pipeline's latency path."""
    return await service.assist(
        ctx.workspace_id,
        ctx.user_id,
        pipeline_type=body.pipeline_type,
        draft_text=body.draft_text,
        target=body.target,
    )


@router.get("/nudge")
@limiter.limit("120/minute")
async def get_nudge(
    request: Request,
    pipeline_type: str = Query("text"),
    piece_id: Optional[str] = Query(None),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Lightweight, LLM-free alignment read for a piece — the same data attached
    inline to pipeline responses, exposed for the frontend to poll."""
    return await service.nudge(ctx.workspace_id, ctx.user_id, piece_id=piece_id)
