"""Routes for the trust controls of video: the workspace's privacy switch, and consent records for voices and faces."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.video import governance

router = APIRouter()
_MANAGE = require("manage_workspace_settings")


class PrivacyBody(BaseModel):
    keep_audio_private: bool


@router.get("/privacy")
@limiter.limit("60/minute")
async def get_privacy(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return await governance.privacy(ctx.workspace_id)


@router.put("/privacy")
@limiter.limit("20/minute")
async def put_privacy(request: Request, body: PrivacyBody, ctx: WorkspaceContext = Depends(_MANAGE)) -> dict[str, Any]:
    """An owner or admin keeps this workspace's audio away from outside speech services. While it is on, writing out words is refused."""
    return await governance.set_privacy(ctx.workspace_id, body.keep_audio_private, ctx.user_id)


class ConsentBody(BaseModel):
    kind: Literal["voice", "face"]
    person: str = Field(..., min_length=1, max_length=120)
    scope: str = Field(..., max_length=500)
    evidence: str = Field(..., max_length=500)
    given_on: str = Field(..., max_length=10)


@router.get("/consents")
@limiter.limit("60/minute")
async def consents(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return {"consents": await governance.list_consents(ctx.workspace_id)}


@router.post("/consents", status_code=201)
@limiter.limit("20/minute")
async def add_consent(request: Request, body: ConsentBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    try:
        return await governance.record_consent(ctx.workspace_id, ctx.user_id, kind=body.kind, person=body.person, scope=body.scope, evidence=body.evidence, given_on=body.given_on)
    except governance.ConsentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/consents/{consent_id}", status_code=204)
@limiter.limit("20/minute")
async def withdraw_consent(request: Request, consent_id: str, ctx: WorkspaceContext = Depends(require("create_content"))) -> Response:
    """Withdraw consent. It stays on the record, and from then on it no longer counts."""
    if not await governance.withdraw(ctx.workspace_id, consent_id, ctx.user_id):
        raise HTTPException(status_code=404, detail="That consent was not found or is already withdrawn.")
    return Response(status_code=204)
