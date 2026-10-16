"""
Content management endpoints — sessions, pieces, versions.

Workspace-scoped. Reads require workspace membership; edits/deletes/restores
require ``edit_content``; approve / reject / schedule / approve-all require
``approve_content`` (owner or admin only).
"""

import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import Field, BaseModel

from app.core.middleware import limiter
from app.core import limits
from app.shared.heavy_work import run_heavy
from app.shared.safe_fetch import ResponseTooLarge, get_limited
from app.shared.tier_policy import blocks_self_approval
from app.core.notifications import send_templated_email
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import audio_assets, brand_profiles, content_pieces, content_sessions, image_assets, media_assets, users
from app.models.media import MediaAsset
from app.models.attachment import AttachRequest, RefreshAttachmentsRequest, ReorderAttachmentsRequest, UpdateAttachmentRequest
from app.pipelines.export import library_archive
from app.pipelines.publish import attachments as piece_attachments
from app.pipelines.publish.registry import get_publisher
from app.pipelines.publish.spine import (
    check_gate,
    parse_schedule_time,
    platform_key,
    promote_approved_intent,
    record_override,
    availability_block,
    schedule_blocker,
)
from app.pipelines.text.storage import (
    get_session,
    get_workspace_sessions,
    get_workspace_pieces,
    get_all_workspace_pieces,
    get_piece,
    update_piece_content,
    update_piece_status,
    approve_all_pieces,
    delete_piece,
    get_versions,
    restore_version,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def _notify_approval_decision(piece: dict, ctx: WorkspaceContext, action_type: str, piece_id: str) -> None:
    """Email the content creator that their piece was approved/rejected.

    Skipped when the creator is the one who made the decision — no one needs
    an email about their own action.
    """
    creator_id = piece.get("user_id")
    if not creator_id or creator_id == ctx.user_id:
        return
    creator = await users.find_one({"id": creator_id}, {"email": 1})
    if creator and creator.get("email"):
        await send_templated_email(
            "content-approved-rejected",
            creator["email"],
            {
                "ACTION_TYPE": action_type,
                "ACTED_BY_NAME": ctx.user.get("name", ""),
                "WORKSPACE_NAME": ctx.workspace.get("name", "your workspace"),
                "PIECE_ID": piece_id,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST / RESPONSE MODELS
# ─────────────────────────────────────────────────────────────────────────────

class EditPieceRequest(BaseModel):
    # Any of the three can be sent. `content` is the post's text; `seo` the details of a Blog or Newsletter post (title or subject,
    # summary or preview line, tags); `publish_options` the settings for the platform it is going to (see pipelines/publish/options.py).
    content: Optional[str] = None
    seo: Optional[dict] = None
    publish_options: Optional[dict] = None
    # The version of the text this edit started from. When sent and the post has moved on (another tab, another member), the edit is
    # refused instead of silently replacing the newer text.
    base_version: Optional[int] = None


class PieceDocRequest(BaseModel):
    """The block form of a post, as the rich editor saves it."""
    doc: dict
    # The version of the text this started from, so a save never silently replaces a newer text (same rule as a plain edit).
    base_version: Optional[int] = None


class UpdatePieceMediaRequest(BaseModel):
    # Empty = remove media (Row 8's "remove" control). One id = swap to that
    # already-uploaded asset (Row 8's "swap" control, after a POST
    # /api/v1/media upload). Never more than one — a piece carries a single
    # attached visual today, same as the default-image picker's own output.
    media_id: Optional[str] = None


class ApprovePieceRequest(BaseModel):
    approval_status: str  # "approved" or "rejected"


class SchedulePieceRequest(BaseModel):
    scheduled_at: str     # ISO datetime string
    # A scheduled YouTube upload publishes with the title, description and
    # tags the member reviewed, not ones made up at upload time.
    youtube_metadata: Optional[dict] = None
    # A flagged or not-quality-passed post is refused unless the member
    # explicitly chooses to send it anyway. Who chose, and when, is kept.
    confirm_publish_anyway: bool = False
    # A newsletter set to go to a whole audience is scheduled only when the member confirmed that.
    confirm_send: bool = False


class MarkPostedRequest(BaseModel):
    post_url: Optional[str] = None


class ArchivePieceRequest(BaseModel):
    archived: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# SESSION ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sessions")
@limiter.limit("60/minute")
async def list_sessions(
    request: Request,
    brand_id: Optional[str] = Query(None),
    is_repurpose: Optional[bool] = Query(None),
    standalone: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """List all content sessions in the active workspace. ``standalone`` leaves out the ones a campaign made."""
    return await get_workspace_sessions(
        workspace_id=ctx.workspace_id,
        brand_id=brand_id,
        is_repurpose=is_repurpose,
        standalone=standalone,
        page=page,
        limit=limit,
    )


class RenameRequest(BaseModel):
    title: str


@router.patch("/sessions/{session_id}")
@limiter.limit("30/minute")
async def rename_session(
    request: Request,
    session_id: str,
    body: RenameRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Give a text run a name of the member's own. It then shows in History instead of the first words of the input."""
    from app.shared.titles import clean_title

    title = clean_title(body.title)
    done = await content_sessions.update_one(
        {"session_id": session_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
        {"$set": {"title": title, "updated_at": datetime.now(timezone.utc)}},
    )
    if done.matched_count == 0:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"session_id": session_id, "title": title}


@router.delete("/sessions/{session_id}")
@limiter.limit("20/minute")
async def remove_session(
    request: Request,
    session_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Remove a text run from History with the posts that were never published. A post that is already out stays in Review, so
    its results are kept. A run with a post that is scheduled or going out is refused: cancel that first."""
    session = await content_sessions.find_one({"session_id": session_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    pieces = await content_pieces.find(
        {"session_id": session_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}, {"piece_id": 1, "publish_status": 1},
    ).to_list(length=500)
    if any(p.get("publish_status") in ("queued", "publishing", "scheduled") for p in pieces):
        raise HTTPException(status_code=409, detail="A post in this run is scheduled or going out. Cancel it first, then delete the run.")
    removable = [p["piece_id"] for p in pieces if p.get("publish_status") != "published"]
    now = datetime.now(timezone.utc)
    if removable:
        await content_pieces.update_many(
            {"piece_id": {"$in": removable}, "workspace_id": ctx.workspace_id}, {"$set": {"deleted": True, "updated_at": now}},
        )
    await content_sessions.update_one(
        {"session_id": session_id, "workspace_id": ctx.workspace_id}, {"$set": {"deleted": True, "updated_at": now}},
    )
    return {"session_id": session_id, "deleted": True, "posts_removed": len(removable), "posts_kept": len(pieces) - len(removable)}


@router.get("/sessions/{session_id}/export")
@limiter.limit("10/minute")
async def export_session(
    request: Request,
    session_id: str,
    format: str = Query(..., pattern="^(markdown|csv|zip)$"),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Response:
    """The posts of one text run as markdown, csv or a zip (with their media)."""
    session = await get_session(session_id, ctx.workspace_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    pieces = session["pieces"]
    if not pieces:
        raise HTTPException(status_code=404, detail="This run has no posts to export.")
    media_type, _ = _EXPORT_CONTENT_TYPES[format]
    filename = f"recast-run-{session_id[:8]}.{ {'markdown': 'md', 'csv': 'csv', 'zip': 'zip'}[format] }"
    if format == "markdown":
        body: str | bytes = _pieces_to_markdown(pieces)
    elif format == "csv":
        body = _pieces_to_csv(pieces)
    else:
        body = await _build_library_zip(pieces, ctx.workspace_id, session.get("brand_id"), only_for_pieces=True)
    return Response(content=body, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/sessions/{session_id}")
@limiter.limit("60/minute")
async def get_session_detail(
    request: Request,
    session_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Fetch one session with all its pieces."""
    session = await get_session(session_id, ctx.workspace_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


# ─────────────────────────────────────────────────────────────────────────────
# PIECE ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/pieces")
@limiter.limit("60/minute")
async def list_pieces(
    request: Request,
    platform: Optional[str] = Query(None),
    approval_status: Optional[str] = Query(None),
    brand_id: Optional[str] = Query(None),
    stage: Optional[str] = Query(None),
    campaign_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """
    Flat, paginated list of pieces across every session in the workspace,
    most recent first. Powers Drafts and Library (Module 2 Stage 8) — both
    need the real piece history, not grouped by session the way
    /sessions/{id} returns it. Each item includes a derived ``stage``
    (drafting/staging/scheduled/published/archived) and resolved
    ``author_name``/``brand_name`` for display. ``campaign_id`` powers the
    real Pipeline page's per-campaign branch view.
    """
    return await get_workspace_pieces(
        workspace_id=ctx.workspace_id,
        page=page,
        limit=limit,
        platform=platform,
        approval_status=approval_status,
        brand_id=brand_id,
        stage=stage,
        campaign_id=campaign_id,
    )


@router.get("/pieces/{piece_id}")
@limiter.limit("60/minute")
async def get_piece_detail(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Fetch one piece by ID."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    return piece


@router.get("/pieces/{piece_id}/doc")
@limiter.limit("120/minute")
async def get_piece_doc(request: Request, piece_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """The block form of a post for the rich editor. A post never opened there is built from its plain text and marked `derived`;
    nothing changes until it is saved."""
    from app.pipelines.text import doc as text_doc

    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    stored = piece.get("content_doc")
    if isinstance(stored, dict) and stored.get("blocks"):
        try:
            return {"doc": text_doc.validate(stored), "derived": False, "version": piece.get("version_count", 1)}
        except text_doc.DocError:
            pass
    return {"doc": text_doc.from_plain(piece.get("content", "")), "derived": True, "version": piece.get("version_count", 1)}


@router.put("/pieces/{piece_id}/doc")
@limiter.limit("60/minute")
async def put_piece_doc(
    request: Request, piece_id: str, body: PieceDocRequest, ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Save a post from the rich editor: the blocks are checked, the plain text is made from them and saved as a new version exactly
    like a plain edit (so publishing, checks and history work as they always did), and the blocks are kept beside it."""
    from app.pipelines.text import doc as text_doc

    try:
        clean = text_doc.validate(body.doc)
    except text_doc.DocError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    plain = text_doc.to_plain(clean)
    if not plain.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty.")
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    if piece.get("publish_status") in ("published", "publishing"):
        raise HTTPException(status_code=409, detail="This post is already published or being published, so its text can't change.")
    if body.base_version is not None and body.base_version != piece.get("version_count", 1):
        raise HTTPException(status_code=409, detail="This post was changed somewhere else while you were editing. Reload it to see the latest text.")
    if plain.strip() == (piece.get("content") or "").strip():
        # the words did not change (only the structure or formatting): keep the blocks without making a new version
        await content_pieces.update_one(
            {"piece_id": piece_id, "workspace_id": ctx.workspace_id},
            {"$set": {"content_doc": clean, "updated_at": datetime.now(timezone.utc)}},
        )
        fresh = await get_piece(piece_id, ctx.workspace_id) or piece
        return {"doc": clean, "version": fresh.get("version_count", 1), "piece": fresh}
    updated = await update_piece_content(
        piece_id=piece_id, workspace_id=ctx.workspace_id, new_content=plain.strip(),
        action="manual_edit", instruction="User edited content in the rich editor", actor_user_id=ctx.user_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": ctx.workspace_id},
        {"$set": {"content_doc": clean}},
    )
    if piece.get("approval_status") == "approved":
        await content_pieces.update_one(
            {"piece_id": piece_id, "workspace_id": ctx.workspace_id},
            {"$set": {"approval_status": "pending", "updated_at": datetime.now(timezone.utc)}},
        )
    fresh = await get_piece(piece_id, ctx.workspace_id) or updated
    return {"doc": clean, "version": fresh.get("version_count", 1), "piece": fresh}


@router.patch("/pieces/{piece_id}")
@limiter.limit("30/minute")
async def edit_piece(
    request: Request,
    piece_id: str,
    body: EditPieceRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Edit a post inline: its text (which creates a new version), its Blog or Newsletter details, or its platform settings."""
    from app.pipelines.publish import options as publish_options

    if body.content is None and body.seo is None and body.publish_options is None:
        raise HTTPException(status_code=400, detail="Send the text, the details or the settings to change.")
    if body.content is not None and not body.content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty.")

    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    if body.seo is not None or body.publish_options is not None:
        if piece.get("publish_status") in ("published", "publishing"):
            raise HTTPException(status_code=409, detail="This post is already published or being published, so it can't change.")
        updates: dict = {}
        try:
            if body.seo is not None:
                for key, value in publish_options.clean_seo(body.seo).items():
                    updates[f"seo.{key}"] = value
            if body.publish_options is not None:
                updates["publish_options"] = publish_options.clean_options(platform_key(piece.get("platform", "")), body.publish_options)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if updates:
            updates["updated_at"] = datetime.now(timezone.utc)
            await content_pieces.update_one({"piece_id": piece_id, "workspace_id": ctx.workspace_id}, {"$set": updates})

    updated = piece
    if body.content is not None and piece.get("publish_status") in ("published", "publishing"):
        # What went out is kept as it was sent; the stored text must keep matching it.
        raise HTTPException(status_code=409, detail="This post is already published or being published, so its text can't change.")
    if body.content is not None and body.base_version is not None and body.base_version != piece.get("version_count", 1):
        raise HTTPException(status_code=409, detail="This post was changed somewhere else while you were editing. Reload it to see the latest text.")
    if body.content is not None:
        updated = await update_piece_content(
            piece_id=piece_id,
            workspace_id=ctx.workspace_id,
            new_content=body.content.strip(),
            action="manual_edit",
            instruction="User edited content manually",
            actor_user_id=ctx.user_id,
        )
        if updated and piece.get("approval_status") == "approved":
            # A post that was approved and is then rewritten has not been reviewed in its new form: it goes back to waiting, and the
            # scheduled sender only sends approved posts.
            await content_pieces.update_one(
                {"piece_id": piece_id, "workspace_id": ctx.workspace_id},
                {"$set": {"approval_status": "pending", "updated_at": datetime.now(timezone.utc)}},
            )
            updated = await get_piece(piece_id, ctx.workspace_id) or updated
    else:
        updated = await get_piece(piece_id, ctx.workspace_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
    return updated


@router.patch("/pieces/{piece_id}/media")
@limiter.limit("30/minute")
async def update_piece_media(
    request: Request,
    piece_id: str,
    body: UpdatePieceMediaRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Row 8 — swap or remove a piece's attached visual (the default-image
    picker's output, or a previous manual attach). No version history —
    unlike edit_piece's text edits, this isn't a content-quality trail
    worth diffing, just which image is currently attached."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")

    media: list[dict] = []
    if body.media_id:
        asset_doc = await media_assets.find_one({
            "id": body.media_id, "workspace_id": ctx.workspace_id,
        })
        if not asset_doc:
            raise HTTPException(status_code=404, detail="Media not found.")
        media = [MediaAsset(**asset_doc).model_dump()]

    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": ctx.workspace_id},
        {"$set": {"media": media, "updated_at": datetime.now(timezone.utc)}},
    )
    # Keep the post's attachment list in step with what it now carries.
    await piece_attachments.replace_all_with_media(piece, ctx.workspace_id, ctx.user_id, body.media_id)
    return await get_piece(piece_id, ctx.workspace_id)


@router.post("/pieces/{piece_id}/check-link")
@limiter.limit("20/minute")
async def check_piece_link(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Ask the platform whether this published post is still there. Returns its state: live, removed (the platform no longer
    shows it), unreachable (the connection to the platform is refused) or unknown (it could not be asked)."""
    from app.pipelines.analytics import link_health

    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    if piece.get("publish_status") != "published" or not piece.get("platform_post_id"):
        raise HTTPException(status_code=400, detail="Only a published post can be checked.")
    return {"piece_id": piece_id, **await link_health.check_now(ctx.workspace_id, piece)}


@router.post("/pieces/{piece_id}/regenerate-picture")
@limiter.limit("6/minute")
async def regenerate_piece_picture(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Make this post's picture again. A new real picture replaces the current one. If the new one comes out as a text
    card and the post already has a real picture, the real one is kept.

    Returns {"state": "ready" | "kept" | "card" | "failed", "note": str | None, "piece": the post}."""
    from app.pipelines.media.default_image import _scene_topic
    from app.pipelines.media.image_generation import generate_brand_image, last_failure_reason

    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece or not (piece.get("content") or "").strip():
        raise HTTPException(status_code=404, detail="Piece not found.")
    brand = await brand_profiles.find_one({"id": piece.get("brand_id"), "workspace_id": ctx.workspace_id}) if piece.get("brand_id") else None
    if not brand:
        raise HTTPException(status_code=400, detail="This post has no brand voice to make a picture from.")

    asset = await generate_brand_image(
        topic=_scene_topic(piece["content"]), brand_profile=brand,
        workspace_id=ctx.workspace_id, user_id=ctx.user_id,
    )
    if asset is None:
        return {"state": "failed", "note": last_failure_reason() or "The picture could not be made. Try again in a moment.", "piece": piece}

    current = piece.get("media") or []
    has_real = any(not m.get("qa_flagged") and m.get("source") != "generated_template" for m in current)
    if asset.qa_flagged and has_real:
        return {"state": "kept", "note": asset.qa_flag_reason, "piece": piece}

    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": ctx.workspace_id},
        {"$set": {"media": [asset.model_dump()], "updated_at": datetime.now(timezone.utc)}},
    )
    await piece_attachments.replace_all_with_media(piece, ctx.workspace_id, ctx.user_id, asset.id)
    return {
        "state": "card" if asset.qa_flagged else "ready",
        "note": asset.qa_flag_reason,
        "piece": await get_piece(piece_id, ctx.workspace_id),
    }


# ── Attachments: images, audio and video attached to a post ──────────────────
# The post is the only thing that gets published; these are the assets on it.
# See app.pipelines.publish.attachments.

def _attachment_http(exc: piece_attachments.AttachmentError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get("/pieces/{piece_id}/attachments")
@limiter.limit("60/minute")
async def list_piece_attachments(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """What is attached to a post, each with ``stale`` and ``stale_reason``:
    "asset_changed" when the image or recording moved on after it was attached,
    "text_changed" when the post's text was edited after a picture or recording
    made from it was attached, otherwise none."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    items = await piece_attachments.list_with_status(piece, ctx.workspace_id)
    return {"piece_id": piece_id, "attachments": items, "total": len(items)}


@router.post("/pieces/{piece_id}/attachments")
@limiter.limit("30/minute")
async def attach_to_piece(
    request: Request,
    piece_id: str,
    body: AttachRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Attach an image, recording, video or uploaded file to a post. The first
    attachment is the one that gets published. Attaching the same thing again
    returns the attachment already there."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    try:
        resolved = await piece_attachments.resolve_asset(
            ctx.workspace_id, body.asset_type, asset_id=body.asset_id,
            slide_number=body.slide_number, clip_id=body.clip_id, media_id=body.media_id,
        )
        attachment, created = await piece_attachments.attach(piece, ctx.workspace_id, ctx.user_id, resolved, alt_text=body.alt_text)
    except piece_attachments.AttachmentError as exc:
        raise _attachment_http(exc)
    fresh = await get_piece(piece_id, ctx.workspace_id) or piece
    items = await piece_attachments.list_with_status(fresh, ctx.workspace_id)
    return {
        "attachment": next((a for a in items if a["id"] == attachment["id"]), attachment),
        "created": created,
        "attachments": items,
        # Raw audio can't be posted by itself; say so now, not at publish time.
        "warning": piece_attachments.AUDIO_NOTE if resolved["asset_type"] == "audio" else None,
        # advice about how well this suits the post's platform (shape, length); never blocks
        "notes": piece_attachments.attach_advice(resolved, fresh),
    }


@router.put("/pieces/{piece_id}/attachments/order")
@limiter.limit("30/minute")
async def reorder_piece_attachments(
    request: Request,
    piece_id: str,
    body: ReorderAttachmentsRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Put a post's attachments in a new order. The first one is the primary picture."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    try:
        await piece_attachments.reorder(piece, ctx.workspace_id, body.attachment_ids)
    except piece_attachments.AttachmentError as exc:
        raise _attachment_http(exc)
    fresh = await get_piece(piece_id, ctx.workspace_id) or piece
    items = await piece_attachments.list_with_status(fresh, ctx.workspace_id)
    return {"piece_id": piece_id, "attachments": items, "total": len(items)}


@router.patch("/pieces/{piece_id}/attachments/{attachment_id}")
@limiter.limit("60/minute")
async def update_piece_attachment(
    request: Request,
    piece_id: str,
    attachment_id: str,
    body: UpdateAttachmentRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Write or clear the description (alt text) of one attached picture."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    try:
        await piece_attachments.set_alt_text(piece, ctx.workspace_id, attachment_id, body.alt_text)
    except piece_attachments.AttachmentError as exc:
        raise _attachment_http(exc)
    fresh = await get_piece(piece_id, ctx.workspace_id) or piece
    items = await piece_attachments.list_with_status(fresh, ctx.workspace_id)
    return {"piece_id": piece_id, "attachment": next((a for a in items if a["id"] == attachment_id), None), "attachments": items}


@router.delete("/pieces/{piece_id}/attachments/{attachment_id}")
@limiter.limit("30/minute")
async def detach_from_piece(
    request: Request,
    piece_id: str,
    attachment_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Take an attachment off a post."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    try:
        await piece_attachments.detach(piece, ctx.workspace_id, attachment_id)
    except piece_attachments.AttachmentError as exc:
        raise _attachment_http(exc)
    fresh = await get_piece(piece_id, ctx.workspace_id) or piece
    items = await piece_attachments.list_with_status(fresh, ctx.workspace_id)
    return {"piece_id": piece_id, "detached": attachment_id, "attachments": items}


@router.post("/pieces/{piece_id}/attachments/refresh")
@limiter.limit("30/minute")
async def refresh_piece_attachments(
    request: Request,
    piece_id: str,
    body: Optional[RefreshAttachmentsRequest] = None,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Bring attachments up to the asset's current version (a newer picture or
    recording). Refused once the post is published or being published."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    try:
        _, problems = await piece_attachments.refresh(piece, ctx.workspace_id, body.asset_id if body else None)
    except piece_attachments.AttachmentError as exc:
        raise _attachment_http(exc)
    fresh = await get_piece(piece_id, ctx.workspace_id) or piece
    items = await piece_attachments.list_with_status(fresh, ctx.workspace_id)
    return {"piece_id": piece_id, "attachments": items, "problems": problems}


@router.patch("/pieces/{piece_id}/approve")
@limiter.limit("30/minute")
async def approve_piece(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Mark a piece as approved."""
    existing = await get_piece(piece_id, ctx.workspace_id)
    if existing and await blocks_self_approval(ctx.workspace_id, ctx.user_id, existing.get("user_id")):
        raise HTTPException(status_code=403, detail="Someone else needs to approve this one, since you wrote it.")
    updated = await update_piece_status(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        approval_status="approved",
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
    # A post generated with a planned time is only queued once it is approved.
    if await promote_approved_intent(updated, ctx.workspace_id):
        updated = await get_piece(piece_id, ctx.workspace_id) or updated
    await _notify_approval_decision(updated, ctx, "approved", piece_id)
    return updated


@router.patch("/pieces/{piece_id}/reject")
@limiter.limit("30/minute")
async def reject_piece(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Mark a piece as rejected."""
    updated = await update_piece_status(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        approval_status="rejected",
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
    await _notify_approval_decision(updated, ctx, "rejected", piece_id)
    return updated


@router.patch("/pieces/{piece_id}/schedule")
@limiter.limit("30/minute")
async def schedule_piece(
    request: Request,
    piece_id: str,
    body: SchedulePieceRequest,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """
    Schedule a piece for real future publishing.

    A piece is always exactly one platform (``piece["platform"]``), so unlike
    the old app.api.v1.publish /schedule this needs no separate platform
    param — and unlike that endpoint's ``publish_status="scheduled"``, this
    writes ``"queued"``, the value app.workers.scheduled_posts actually
    polls for. Writing "scheduled" (or skipping the token/content checks
    below) is exactly how a piece used to end up permanently stuck looking
    scheduled in the UI while the worker silently never picked it up.
    """
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")

    # A post that is out (or on its way out) must never be queued again: it
    # would be posted a second time and orphan the first post's results.
    if piece.get("publish_status") in ("published", "publishing"):
        raise HTTPException(status_code=409, detail="This post is already published or being published.")

    platform = piece["platform"]
    slug = platform_key(platform)

    # Real UTC datetime, so the worker's comparison is a date comparison and
    # not a string one; a time already well past is refused up front.
    scheduled_at = parse_schedule_time(body.scheduled_at)

    unavailable = await availability_block(platform, ctx.workspace_id)
    if unavailable:
        raise unavailable.http()
    blocker = await schedule_blocker(piece, ctx.workspace_id)
    if blocker:
        raise HTTPException(status_code=blocker[0], detail=blocker[1])

    if slug == "youtube" and body.youtube_metadata is not None:
        from app.pipelines.publish.youtube.metadata import YouTubeMetadata, visibility_problem
        try:
            reviewed = YouTubeMetadata(**body.youtube_metadata)
        except Exception:
            raise HTTPException(status_code=422, detail="The YouTube details aren't valid. Check the title, tags and category.")
        problem = visibility_problem(reviewed.privacy_status)
        if problem:
            raise HTTPException(status_code=422, detail=problem)

    block = check_gate(piece, confirm_anyway=body.confirm_publish_anyway)
    if block:
        raise block.http()
    if body.confirm_publish_anyway:
        await record_override(piece, ctx.workspace_id, ctx.user_id)

    from app.pipelines.publish.destinations import service as destination_service

    audience_send = destination_service.sends_to_audience(piece)
    if audience_send and not body.confirm_send:
        raise HTTPException(status_code=422, detail="Confirm that this should be sent to the whole audience at that time.")

    # One conditional write: it only queues a post that is not published or being published at this instant, so a send that was
    # claimed while the checks above ran is never overwritten back to queued (which would post it a second time). A new schedule
    # also starts the retry count over and drops the trimmed text and recovery flag a past failure left behind.
    queued = await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": ctx.workspace_id, "publish_status": {"$nin": ["published", "publishing"]}},
        {
            "$set": {
                "publish_status": "queued", "publish_scheduled_at": scheduled_at, "publish_target": slug,
                "publish_attempts": 0, "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {"schedule_note": "", "publish_content_override": "", "auth_recovery_tried": "", "last_error": "", "last_error_code": ""},
        },
    )
    if not queued.matched_count:
        raise HTTPException(status_code=409, detail="This post is already published or being published.")
    updated = await get_piece(piece_id, ctx.workspace_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
    # The confirmation to send to a whole audience is kept with the schedule, so the worker never sends without it.
    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": ctx.workspace_id}, {"$set": {"publish_send_confirmed": bool(audience_send)}},
    )
    if slug == "youtube" and body.youtube_metadata is not None:
        await content_pieces.update_one(
            {"piece_id": piece_id, "workspace_id": ctx.workspace_id},
            {"$set": {"publish_youtube_metadata": body.youtube_metadata}},
        )
        updated["publish_youtube_metadata"] = body.youtube_metadata
    return updated


@router.post("/pieces/{piece_id}/mark-posted")
@limiter.limit("30/minute")
async def mark_piece_posted(
    request: Request,
    piece_id: str,
    body: MarkPostedRequest,
    ctx: WorkspaceContext = Depends(require("publish_content")),
) -> dict:
    """
    Record that the member posted this themselves, for a platform Ekavo has no
    publisher for (Twitter/X, Blog, Newsletter). Without this those posts could
    never reach Published, so they sat in Review forever and never showed up in
    the calendar or history. A platform Ekavo can post to is refused, so this
    can't be used to skip a real publish.
    """
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")

    platform = piece["platform"]
    try:
        get_publisher(platform)
    except ValueError:
        pass
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Ekavo can post to {platform} for you. Use Publish Now instead.",
        )

    if piece.get("publish_status") == "published":
        raise HTTPException(status_code=400, detail="This post is already marked as published.")
    if piece.get("approval_status") != "approved":
        raise HTTPException(status_code=400, detail="Move this post to Review first.")

    post_url = (body.post_url or "").strip()
    if post_url and not post_url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="The link must start with https://")

    now = datetime.now(timezone.utc)
    updates: dict = {
        "publish_status": "published",
        "published_at": now,
        "published_manually": True,
        "publish_scheduled_at": "",
        "last_error": None,
        "updated_at": now,
    }
    if post_url:
        updates["platform_post_url"] = post_url
    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": ctx.workspace_id}, {"$set": updates},
    )

    from app.shared.governance_events import emit_content_published
    emit_content_published(
        ctx.workspace_id, pipeline_type=piece.get("pipeline_type", "text"),
        actor_user_id=ctx.user_id, actor_role=ctx.role,
        content_id=piece_id, target=platform_key(platform), external_url=post_url,
    )
    return await get_piece(piece_id, ctx.workspace_id)


@router.post("/pieces/{piece_id}/cancel-schedule")
@limiter.limit("30/minute")
async def cancel_schedule_piece(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Un-schedule a queued piece — back to pending, not sent to the worker."""
    piece = await get_piece(piece_id, ctx.workspace_id)
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    if piece.get("publish_status") not in ("queued", "publishing", "scheduled", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Piece is '{piece.get('publish_status')}', not scheduled — nothing to cancel.",
        )

    if piece.get("publish_status") == "publishing":
        # The platform is being sent this post right now. Cancelling would not stop it, and the worker's retry could bring it back.
        raise HTTPException(status_code=409, detail="This post is being sent right now, so it can't be cancelled. Check the platform in a minute.")
    cancelled = await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": ctx.workspace_id, "publish_status": {"$in": ["queued", "scheduled", "failed"]}},
        {
            "$set": {
                "publish_status": "pending", "publish_scheduled_at": "", "publish_target": "",
                "publish_attempts": 0, "updated_at": datetime.now(timezone.utc),
            },
            # The reviewed YouTube details, the trimmed text and the recovery flag all belonged to that schedule.
            "$unset": {"publish_youtube_metadata": "", "publish_content_override": "", "auth_recovery_tried": ""},
        },
    )
    if not cancelled.matched_count:
        raise HTTPException(status_code=409, detail="This post changed while you were cancelling it. Refresh and look again.")
    return await get_piece(piece_id, ctx.workspace_id)


@router.patch("/pieces/{piece_id}/archive")
@limiter.limit("30/minute")
async def archive_piece(
    request: Request,
    piece_id: str,
    body: ArchivePieceRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Archive (or unarchive, with archived: false) a piece. A housekeeping
    action distinct from approve/reject — it's the Drafts kanban's lateral
    'Archive' move, available from any stage."""
    updated = await update_piece_status(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        archived=body.archived,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
    return updated


class ApproveManyRequest(BaseModel):
    piece_ids: list[str] = Field(min_length=1, max_length=200)


@router.post("/pieces/approve-many")
@limiter.limit("20/minute")
async def approve_many(
    request: Request,
    body: ApproveManyRequest,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Approve the chosen posts in one call (the board's "approve all" used one request per post and hit the per-minute limit). The same
    rules as approving one at a time: a post that was rejected, or that you wrote where another person must approve, is left and
    named in `skipped` with the reason."""
    approved: list[str] = []
    skipped: list[dict] = []
    queued = 0
    for piece_id in dict.fromkeys(body.piece_ids):
        piece = await get_piece(piece_id, ctx.workspace_id)
        if not piece or piece.get("deleted"):
            skipped.append({"piece_id": piece_id, "reason": "This post no longer exists."})
        elif piece.get("approval_status") == "rejected":
            skipped.append({"piece_id": piece_id, "reason": "This one was rejected, so it was left as it is."})
        elif await blocks_self_approval(ctx.workspace_id, ctx.user_id, piece.get("user_id")):
            skipped.append({"piece_id": piece_id, "reason": "Someone else needs to approve this one, since you wrote it."})
        else:
            updated = await update_piece_status(piece_id=piece_id, workspace_id=ctx.workspace_id, approval_status="approved")
            if updated:
                approved.append(piece_id)
                if await promote_approved_intent(updated, ctx.workspace_id) == "queued":
                    queued += 1
    return {"approved": approved, "skipped": skipped, "queued_count": queued}


@router.post("/approve-waiting")
@limiter.limit("5/minute")
async def approve_waiting(
    request: Request,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Approve every post that is waiting for review in this workspace. Used when the review step is switched off and the member
    chooses to let the waiting posts through too. Rejected posts are not touched. Posts with a planned time are queued, as
    they are when approved one at a time."""
    waiting = await content_pieces.find({
        "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}, "archived": {"$ne": True}, "approval_status": "pending",
    }).limit(500).to_list(length=500)
    approved = queued = skipped = 0
    for doc in waiting:
        # The same rule as approving one post at a time: people do not approve their own work where the workspace says another
        # person must.
        if await blocks_self_approval(ctx.workspace_id, ctx.user_id, doc.get("user_id")):
            skipped += 1
            continue
        updated = await update_piece_status(piece_id=doc["piece_id"], workspace_id=ctx.workspace_id, approval_status="approved")
        if not updated:
            continue
        approved += 1
        if await promote_approved_intent(updated, ctx.workspace_id) == "queued":
            queued += 1
    return {"approved_count": approved, "queued_count": queued, "left_for_someone_else": skipped}


@router.patch("/sessions/{session_id}/approve-all")
@limiter.limit("20/minute")
async def approve_all(
    request: Request,
    session_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Approve all pieces in a session at once."""
    session = await get_session(session_id, ctx.workspace_id)
    if session and await blocks_self_approval(ctx.workspace_id, ctx.user_id, session.get("user_id")):
        raise HTTPException(status_code=403, detail="Someone else needs to approve these, since you wrote them.")
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    count = await approve_all_pieces(session_id, ctx.workspace_id)
    # Posts generated with a planned time are queued now that they are approved.
    queued = 0
    planned = await content_pieces.find({
        "session_id": session_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True},
        "intended_publish_at": {"$ne": None}, "publish_status": {"$in": [None, "pending"]}, "approval_status": "approved",
    }).to_list(length=200)
    for piece in planned:
        if await promote_approved_intent(piece, ctx.workspace_id) == "queued":
            queued += 1
    return {"session_id": session_id, "approved_count": count, "queued_count": queued}


@router.delete("/pieces/{piece_id}")
@limiter.limit("20/minute")
async def remove_piece(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Soft delete a piece."""
    existing = await get_piece(piece_id, ctx.workspace_id)
    if existing and existing.get("publish_status") in ("queued", "publishing"):
        # The same rule deleting a whole run already follows. A post waiting to go out, or going out this moment, is cancelled first;
        # otherwise it could go live and then disappear from the library.
        raise HTTPException(status_code=409, detail="This post is scheduled or being sent. Cancel it first, then you can delete it.")
    deleted = await delete_piece(piece_id, ctx.workspace_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Piece not found.")
    return {"piece_id": piece_id, "deleted": True}


# ─────────────────────────────────────────────────────────────────────────────
# VERSION HISTORY ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/pieces/{piece_id}/versions")
@limiter.limit("60/minute")
async def list_versions(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """List all versions of a piece ordered by version number."""
    versions = await get_versions(piece_id, ctx.workspace_id)
    if not versions:
        piece = await get_piece(piece_id, ctx.workspace_id)
        if not piece:
            raise HTTPException(status_code=404, detail="Piece not found.")
    return {
        "piece_id": piece_id,
        "versions": versions,
        "total": len(versions),
    }


@router.post("/pieces/{piece_id}/restore/{version_number}")
@limiter.limit("20/minute")
async def restore_piece_version(
    request: Request,
    piece_id: str,
    version_number: int,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Restore a piece to a specific version. Creates a new version entry."""
    restored = await restore_version(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        version_number=version_number,
        actor_user_id=ctx.user_id,
    )
    if not restored:
        raise HTTPException(
            status_code=404,
            detail=f"Piece or version {version_number} not found.",
        )
    return restored


# ─────────────────────────────────────────────────────────────────────────────
# EXPORT — real Library download, added 2026-09-26 (standalone-usage audit,
# see pow/audio_image_pipeline/GAPS.md). Library's Markdown/CSV/ZIP buttons
# previously called `handlePlaceholderExport`, an honest mock that never did
# anything. A user who never connects a publish platform needs a real way to
# get their content out — this covers every real piece in the workspace
# (get_all_workspace_pieces has no pagination), read fresh from the DB at
# export time, not whatever a page's already-loaded cache happens to hold.
# ─────────────────────────────────────────────────────────────────────────────

def _piece_header(p: dict) -> str:
    created = p.get("created_at")
    created_str = created.strftime("%Y-%m-%d %H:%M UTC") if isinstance(created, datetime) else str(created or "")
    return f"{p.get('platform', '')} — {p.get('brand_name', '')} — {created_str}"


def _pieces_to_markdown(pieces: list[dict]) -> str:
    if not pieces:
        return "# Ekavo Library Export\n\nNo pieces to export.\n"
    parts = [f"# Ekavo Library Export ({len(pieces)} pieces)\n"]
    for p in pieces:
        parts.append(f"## {_piece_header(p)}\n")
        parts.append(p.get("content", "") + "\n")
        hashtags = p.get("seo", {}).get("hashtags") or []
        if hashtags:
            parts.append("Tags: " + ", ".join(f"#{t}" for t in hashtags) + "\n")
        parts.append("---\n")
    return "\n".join(parts)


def _csv_safe(value) -> str:
    """A cell that starts with = + - or @ would run as a formula when the file is opened in a spreadsheet (a post's text is whatever a
    person or a model wrote), so it is made plain text first."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _pieces_to_csv(pieces: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["piece_id", "platform", "brand_name", "stage", "word_count", "char_count", "created_at", "content"]
    )
    for p in pieces:
        created = p.get("created_at")
        created_str = created.isoformat() if isinstance(created, datetime) else str(created or "")
        writer.writerow([
            _csv_safe(p.get("piece_id", "")),
            _csv_safe(p.get("platform", "")),
            _csv_safe(p.get("brand_name", "")),
            _csv_safe(p.get("stage", "")),
            p.get("word_count", ""),
            p.get("char_count", ""),
            created_str,
            _csv_safe(p.get("content", "")),
        ])
    return buf.getvalue()



#: One export holds its files in memory while the ZIP is built, so it is limited in total and per file. Files over the limit are
#: listed in the README (and stay in the Library), never silently dropped.
EXPORT_MAX_FILE_BYTES = limits.EXPORT_ZIP_FILE_BYTES
EXPORT_MAX_TOTAL_BYTES = limits.EXPORT_ZIP_TOTAL_BYTES


async def _fetch_media(url: str, client: httpx.AsyncClient, limit: asyncio.Semaphore, budget: dict) -> Optional[bytes]:
    """One stored media file, or None if it cannot be downloaded or would take the export past its size budget (never raises)."""
    async with limit:
        remaining = budget["left"]
        if remaining <= 0:
            return None
        try:
            res, body = await get_limited(client, url, min(EXPORT_MAX_FILE_BYTES, remaining))
            res.raise_for_status()
            budget["left"] -= len(body)
            return body
        except ResponseTooLarge:
            logger.info("Library export: %s is too large to include", url)
            return None
        except httpx.HTTPError as exc:
            logger.warning("Library export: couldn't download %s: %s", url, exc)
            return None


async def _build_library_zip(pieces: list[dict], workspace_id: str, brand_id: Optional[str], *, only_for_pieces: bool = False) -> bytes:
    """Posts as individual .txt files in text/, and every audio recording, image and attached media
    in media/ in its real format. Files that cannot be downloaded are listed in README.txt, never
    silently dropped, and never make the whole export fail."""
    query: dict = {"workspace_id": workspace_id}
    if brand_id:
        query["brand_id"] = brand_id
    query = {**query, "deleted": {"$ne": True}}
    if only_for_pieces:
        # One run's export holds that run's own posts and the audio and pictures made for them, not everything else in the workspace.
        query["source_piece_id"] = {"$in": [p.get("piece_id") for p in pieces if p.get("piece_id")]}
    # A recording's word list and a picture's layers are the large parts of these records and an export does not use them.
    audio_docs = await audio_assets.find(query, {"transcript": 0, "approved_master_transcript": 0, "comments": 0}).sort("created_at", -1).to_list(length=500)
    image_docs = await image_assets.find(query, {"slides.layers": 0, "comments": 0}).sort("created_at", -1).to_list(length=500)

    media_ids = {a.get("media_id") for a in audio_docs if a.get("media_id")}
    for a in audio_docs:
        media_ids.update(c.get("media_id") for c in a.get("video_clips") or [] if c.get("media_id"))
    for doc in image_docs:
        media_ids.update(s.get("media_id") for s in doc.get("slides", []) if s.get("media_id"))
    media_docs = await media_assets.find({"id": {"$in": list(media_ids)}, "workspace_id": workspace_id}).to_list(length=None)
    media_by_id = {m["id"]: m for m in media_docs}

    planned = library_archive.plan_library_export(pieces, audio_docs, image_docs, media_by_id)
    planned, skipped = library_archive.within_limits(planned)

    to_fetch = [f for f in planned if f.folder == "media" and f.url]
    fetched: dict[str, bytes] = {}
    failed: list[str] = []
    if to_fetch:
        limit = asyncio.Semaphore(2)
        budget = {"left": EXPORT_MAX_TOTAL_BYTES}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            results = await asyncio.gather(*(_fetch_media(f.url, client, limit, budget) for f in to_fetch))
        for f, data in zip(to_fetch, results):
            if data is None:
                failed.append(f.label or f.name)
            else:
                fetched[f.name] = data
    return await run_heavy(library_archive.build_zip, planned, fetched, failed, skipped)


_EXPORT_CONTENT_TYPES = {
    "markdown": ("text/markdown", "recast-library.md"),
    "csv": ("text/csv", "recast-library.csv"),
    "zip": ("application/zip", "recast-library.zip"),
}


@router.get("/export")
@limiter.limit("10/minute")
async def export_pieces(
    request: Request,
    format: str = Query(..., pattern="^(markdown|csv|zip)$"),
    brand_id: Optional[str] = Query(None),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Response:
    """Real export of every real piece in the workspace (optionally
    filtered to one brand) — markdown/csv/zip. No pagination: the whole
    library, read fresh from the DB, not a partial or stale view."""
    pieces = await get_all_workspace_pieces(workspace_id=ctx.workspace_id, brand_id=brand_id)
    media_type, filename = _EXPORT_CONTENT_TYPES[format]

    if format == "markdown":
        body: str | bytes = _pieces_to_markdown(pieces)
    elif format == "csv":
        body = _pieces_to_csv(pieces)
    else:
        body = await _build_library_zip(pieces, ctx.workspace_id, brand_id)

    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
