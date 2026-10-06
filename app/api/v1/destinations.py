"""Blog and Newsletter destinations: connect a WordPress site, a Ghost site or a Mailchimp account with the member's own details, list what
each offers (categories, authors, audiences) and send a finished post there. Everything else about a post's journey (approval, the
activity log, health marks) goes through the same steps as any other publish; see pipelines/publish/executor.py.

A destination post is sent once, while the member watches. It is not retried or scheduled here: the destination keeps its own drafts and
schedule, which is where a member would expect to manage them.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.pipelines.publish.destinations import ghost, mailchimp, wordpress
from app.pipelines.publish.destinations import service
from app.pipelines.publish.destinations.common import DestinationError
from app.pipelines.publish.destinations.service import DESTINATIONS, LABELS
from app.pipelines.publish.spine import check_gate, platform_key, record_override
from app.pipelines.publish.token_store import get_token, save_token

router = APIRouter()
logger = logging.getLogger(__name__)



class WordPressConnect(BaseModel):
    site_url: str
    username: str
    application_password: str


class GhostConnect(BaseModel):
    site_url: str
    admin_key: str


class MailchimpConnect(BaseModel):
    api_key: str


class SendRequest(BaseModel):
    piece_id: str
    # A flagged post needs an explicit "publish anyway", the same as everywhere else.
    confirm_publish_anyway: bool = False
    # Sending a newsletter to a whole audience needs its own confirmation.
    confirm_send: bool = False


def _refused(exc: DestinationError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/wordpress/connect")
@limiter.limit("10/minute")
async def connect_wordpress(request: Request, body: WordPressConnect, ctx: WorkspaceContext = Depends(require("manage_connections"))) -> dict:
    try:
        facts = await wordpress.verify(body.site_url, body.username.strip(), body.application_password.strip())
    except DestinationError as exc:
        raise _refused(exc)
    await save_token(
        workspace_id=ctx.workspace_id, platform="wordpress", access_token=body.application_password.strip(), refresh_token=None, expires_at=None,
        platform_user_id=facts["site_url"], username=body.username.strip(), connected_by=ctx.user_id, profile_url=facts["site_url"],
    )
    return {"platform": "wordpress", "connected": True, "site": facts["site_url"], "name": facts["name"]}


@router.post("/ghost/connect")
@limiter.limit("10/minute")
async def connect_ghost(request: Request, body: GhostConnect, ctx: WorkspaceContext = Depends(require("manage_connections"))) -> dict:
    try:
        facts = await ghost.verify(body.site_url, body.admin_key.strip())
    except DestinationError as exc:
        raise _refused(exc)
    await save_token(
        workspace_id=ctx.workspace_id, platform="ghost", access_token=body.admin_key.strip(), refresh_token=None, expires_at=None,
        platform_user_id=facts["site_url"], username=facts["name"], connected_by=ctx.user_id, profile_url=facts["site_url"],
    )
    return {"platform": "ghost", "connected": True, "site": facts["site_url"], "name": facts["name"]}


@router.post("/mailchimp/connect")
@limiter.limit("10/minute")
async def connect_mailchimp(request: Request, body: MailchimpConnect, ctx: WorkspaceContext = Depends(require("manage_connections"))) -> dict:
    try:
        facts = await mailchimp.verify(body.api_key.strip())
    except DestinationError as exc:
        raise _refused(exc)
    await save_token(
        workspace_id=ctx.workspace_id, platform="mailchimp", access_token=body.api_key.strip(), refresh_token=None, expires_at=None,
        platform_user_id=facts["data_centre"], username=facts["name"], connected_by=ctx.user_id,
    )
    return {"platform": "mailchimp", "connected": True, "name": facts["name"]}


async def _connection(ctx: WorkspaceContext, key: str) -> dict:
    token = await get_token(ctx.workspace_id, key)
    if not token:
        raise HTTPException(status_code=400, detail=f"{LABELS[key]} is not connected. Connect it first.")
    return token


@router.get("/wordpress/choices")
@limiter.limit("30/minute")
async def wordpress_choices(request: Request, ctx: WorkspaceContext = Depends(require("publish_content"))) -> dict:
    """The categories and authors a WordPress post can use. Either list is empty when the site does not share it."""
    token = await _connection(ctx, "wordpress")
    site, username, password = token["platform_user_id"], token["username"], token["access_token"]
    return {"categories": await wordpress.categories(site, username, password), "authors": await wordpress.authors(site, username, password)}


@router.get("/mailchimp/audiences")
@limiter.limit("30/minute")
async def mailchimp_audiences(request: Request, ctx: WorkspaceContext = Depends(require("publish_content"))) -> dict:
    token = await _connection(ctx, "mailchimp")
    return {"audiences": await mailchimp.audiences(token["access_token"])}


@router.post("/send")
@limiter.limit("10/minute")
async def send_to_destination(request: Request, body: SendRequest, ctx: WorkspaceContext = Depends(require("publish_content"))) -> dict:
    """Sends a Blog or Newsletter post to the destination chosen on it."""
    from app.api.v1.publish import _claim_piece_for_publishing, _record_publish_failure, _release_claim
    from app.agents.content_guard.agent import check_piece_before_send

    ws = ctx.workspace_id
    piece, previous = await _claim_piece_for_publishing(body.piece_id, ws)

    async def refuse(status: int, detail: str):
        await _release_claim(body.piece_id, ws, previous)
        raise HTTPException(status_code=status, detail=detail)

    if platform_key(piece["platform"]) not in DESTINATIONS:
        await refuse(400, "Only Blog and Newsletter posts can be sent to a destination.")
    destination = service.destination_of(piece)
    if not destination:
        await refuse(400, "Choose where to publish this post first.")

    await check_piece_before_send(piece, ws)
    block = check_gate(piece, confirm_anyway=body.confirm_publish_anyway)
    if block:
        await _release_claim(body.piece_id, ws, previous)
        raise block.http()
    if body.confirm_publish_anyway:
        await record_override(piece, ws, ctx.user_id)

    try:
        result = await service.send_piece(piece, ws, may_send_to_audience=body.confirm_send)
    except DestinationError as exc:
        await refuse(422 if "Confirm" in str(exc) or "Choose which" in str(exc) or "title" in str(exc) else 400, str(exc))

    if result.success:
        await service.record_success(piece, ws, result, user_id=ctx.user_id, role=ctx.role)
        return {
            "success": True, "destination": destination, "platform_post_url": result.platform_post_url, "live": service.is_live(piece),
            "note": result.media_dropped_reason,
        }

    from datetime import datetime, timezone

    from app.db.mongo import content_pieces

    await content_pieces.update_one(
        {"piece_id": piece["piece_id"], "workspace_id": ws, "publish_status": "publishing"},
        {"$set": {"publish_status": "failed", "last_error": result.error_message, "updated_at": datetime.now(timezone.utc)}, "$inc": {"publish_attempts": 1}},
    )
    await _record_publish_failure(ws, ctx.user_id, piece["piece_id"], destination, result.error_message or "")
    if result.error_type == "AUTH":
        from app.pipelines.publish.health import record_failure

        await record_failure(ws, destination, reason="the destination refused the saved details", broken=True)
    raise HTTPException(status_code=409 if result.error_type == "AUTH" else 502, detail=result.error_message or "The destination did not accept the post.")
