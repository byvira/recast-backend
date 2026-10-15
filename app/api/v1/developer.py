"""Where an owner or admin manages the public API: workspace keys and webhooks. The key and the webhook secret are shown once, in the
answer that makes them, and never again."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.video import api_keys, webhooks

router = APIRouter()
_MANAGE = require("manage_workspace_settings")


class KeyBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    scopes: list[str] = Field(default_factory=lambda: ["video:read"], max_length=4)


@router.get("/keys")
@limiter.limit("60/minute")
async def list_keys(request: Request, ctx: WorkspaceContext = Depends(_MANAGE)) -> dict[str, Any]:
    return {"keys": await api_keys.list_keys(ctx.workspace_id), "scopes": list(api_keys.SCOPES)}


@router.post("/keys", status_code=201)
@limiter.limit("10/minute")
async def create_key(request: Request, body: KeyBody, ctx: WorkspaceContext = Depends(_MANAGE)) -> dict[str, Any]:
    try:
        record, plain = await api_keys.create(ctx.workspace_id, ctx.user_id, body.name, body.scopes)
    except api_keys.KeyError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"key": record, "secret": plain, "note": "Copy this key now. It is not shown again."}


@router.delete("/keys/{key_id}", status_code=204)
@limiter.limit("30/minute")
async def revoke_key(request: Request, key_id: str, ctx: WorkspaceContext = Depends(_MANAGE)) -> Response:
    if not await api_keys.revoke(ctx.workspace_id, key_id):
        raise HTTPException(status_code=404, detail="That key was not found.")
    return Response(status_code=204)


class HookBody(BaseModel):
    url: str = Field(..., min_length=8, max_length=500)
    events: list[str] = Field(..., min_length=1, max_length=5)


@router.get("/webhooks")
@limiter.limit("60/minute")
async def list_webhooks(request: Request, ctx: WorkspaceContext = Depends(_MANAGE)) -> dict[str, Any]:
    return {"webhooks": await webhooks.list_hooks(ctx.workspace_id), "events": list(webhooks.EVENTS)}


@router.post("/webhooks", status_code=201)
@limiter.limit("10/minute")
async def create_webhook(request: Request, body: HookBody, ctx: WorkspaceContext = Depends(_MANAGE)) -> dict[str, Any]:
    try:
        record, secret = await webhooks.create(ctx.workspace_id, ctx.user_id, body.url, body.events)
    except webhooks.WebhookError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"webhook": record, "signing_secret": secret, "note": "Copy the signing secret now. It is not shown again."}


@router.delete("/webhooks/{hook_id}", status_code=204)
@limiter.limit("30/minute")
async def remove_webhook(request: Request, hook_id: str, ctx: WorkspaceContext = Depends(_MANAGE)) -> Response:
    if not await webhooks.remove(ctx.workspace_id, hook_id):
        raise HTTPException(status_code=404, detail="That webhook was not found.")
    return Response(status_code=204)


@router.post("/webhooks/{hook_id}/test")
@limiter.limit("10/minute")
async def test_webhook(request: Request, hook_id: str, ctx: WorkspaceContext = Depends(_MANAGE)) -> dict[str, Any]:
    try:
        return await webhooks.send_test(ctx.workspace_id, hook_id)
    except webhooks.WebhookError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/webhooks/{hook_id}/deliveries")
@limiter.limit("60/minute")
async def webhook_deliveries(request: Request, hook_id: str, ctx: WorkspaceContext = Depends(_MANAGE)) -> dict[str, Any]:
    return {"deliveries": await webhooks.recent_deliveries(ctx.workspace_id, hook_id)}
