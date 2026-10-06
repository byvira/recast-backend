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
from app.pipelines.publish import executor
from app.pipelines.publish.destinations import ghost, mailchimp, wordpress
from app.pipelines.publish.destinations.common import DestinationError
from app.pipelines.publish.spine import check_gate, platform_key, record_override
from app.pipelines.publish.token_store import get_token, save_token

router = APIRouter()
logger = logging.getLogger(__name__)

#: Which destinations each kind of post can go to.
DESTINATIONS = {"blog": ("wordpress", "ghost"), "newsletter": ("mailchimp",)}
LABELS = {"wordpress": "WordPress", "ghost": "Ghost", "mailchimp": "Mailchimp"}


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


def _title(piece: dict) -> str:
    seo = piece.get("seo") or {}
    if (seo.get("title") or "").strip():
        return seo["title"].strip()
    for line in (piece.get("content") or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:200]
    return ""


def _body(piece: dict) -> str:
    """The post without the heading line the editor copies in front of it, since the title is sent on its own."""
    lines = (piece.get("content") or "").splitlines()
    if lines and lines[0].startswith("# ") and lines[0][2:].strip() == (piece.get("seo") or {}).get("title", "").strip():
        return "\n".join(lines[1:]).lstrip()
    return piece.get("content") or ""


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

    kind = platform_key(piece["platform"])
    options = piece.get("publish_options") or {}
    destination = options.get("destination")
    if kind not in DESTINATIONS:
        await refuse(400, "Only Blog and Newsletter posts can be sent to a destination.")
    if destination not in DESTINATIONS[kind]:
        await refuse(400, "Choose where to publish this post first.")

    await check_piece_before_send(piece, ws)
    block = check_gate(piece, confirm_anyway=body.confirm_publish_anyway)
    if block:
        await _release_claim(body.piece_id, ws, previous)
        raise block.http()
    if body.confirm_publish_anyway:
        await record_override(piece, ws, ctx.user_id)

    token = await get_token(ws, destination)
    if not token:
        await refuse(400, f"{LABELS[destination]} is not connected. Connect it first.")

    title = _title(piece)
    if not title:
        await refuse(422, "Add a title before publishing.")
    seo = piece.get("seo") or {}
    pictures = await executor.pictures_for(piece, ws)

    if destination == "mailchimp":
        audience = options.get("audience_id") or ""
        if not audience:
            await refuse(422, "Choose which Mailchimp audience this goes to.")
        send = options.get("send_mode") == "send"
        if send and not body.confirm_send:
            await refuse(422, "Confirm that this should be sent to the whole audience now.")
        result = await mailchimp.publish(
            api_key=token["access_token"], piece_id=piece["piece_id"], audience_id=audience, subject=title,
            preview=seo.get("meta_description") or "", content=piece.get("content") or "", send=send,
        )
    elif destination == "wordpress":
        result = await wordpress.publish(
            site=token["platform_user_id"], username=token["username"], password=token["access_token"], piece_id=piece["piece_id"],
            title=title, content=_body(piece), excerpt=seo.get("meta_description") or "", slug=seo.get("slug") or "",
            status=options.get("post_status") or "draft", category_id=options.get("category_id") or "", author_id=options.get("author_id") or "",
            pictures=pictures,
        )
    else:
        result = await ghost.publish(
            site=token["platform_user_id"], admin_key=token["access_token"], piece_id=piece["piece_id"], title=title, content=_body(piece),
            excerpt=seo.get("meta_description") or "", slug=seo.get("slug") or "", status=options.get("post_status") or "draft",
            tags=seo.get("tags") or [], pictures=pictures,
        )

    if result.success:
        await executor.mark_published(piece, ws, destination, result, piece.get("content") or "", increment_attempts=True)
        await executor.after_published(piece, ws, destination, result, user_id=ctx.user_id, role=ctx.role)
        live = (destination == "mailchimp" and options.get("send_mode") == "send") or options.get("post_status") == "publish"
        from app.db.mongo import content_pieces as pieces

        # A draft left in the destination is still "published" here, so what the member sees must say it is a draft.
        await pieces.update_one(
            {"piece_id": piece["piece_id"], "workspace_id": ws},
            {"$set": {"publish_destination": destination, "publish_destination_state": "live" if live else "draft"}},
        )
        return {
            "success": True, "destination": destination, "platform_post_url": result.platform_post_url, "live": bool(live),
            "note": result.media_dropped_reason,
        }

    from app.db.mongo import content_pieces
    from datetime import datetime, timezone

    await content_pieces.update_one(
        {"piece_id": piece["piece_id"], "workspace_id": ws, "publish_status": "publishing"},
        {"$set": {"publish_status": "failed", "last_error": result.error_message, "updated_at": datetime.now(timezone.utc)}, "$inc": {"publish_attempts": 1}},
    )
    await _record_publish_failure(ws, ctx.user_id, piece["piece_id"], destination, result.error_message or "")
    if result.error_type == "AUTH":
        from app.pipelines.publish.health import record_failure

        await record_failure(ws, destination, reason="the destination refused the saved details", broken=True)
    raise HTTPException(status_code=409 if result.error_type == "AUTH" else 502, detail=result.error_message or "The destination did not accept the post.")
