"""
Content management endpoints — sessions, pieces, versions.

Workspace-scoped. Reads require workspace membership; edits/deletes/restores
require ``edit_content``; approve / reject / schedule / approve-all require
``approve_content`` (owner or admin only).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.middleware import limiter
from app.core.notifications import send_templated_email
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import content_pieces, media_assets, users
from app.models.media import MediaAsset
from app.pipelines.publish.registry import get_publisher
from app.pipelines.publish.token_store import get_token
from app.pipelines.publish.validators import validate_for_platform
from app.pipelines.text.storage import (
    get_session,
    get_workspace_sessions,
    get_workspace_pieces,
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
    content: str


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
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """List all content sessions in the active workspace."""
    return await get_workspace_sessions(
        workspace_id=ctx.workspace_id,
        brand_id=brand_id,
        is_repurpose=is_repurpose,
        page=page,
        limit=limit,
    )


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


@router.patch("/pieces/{piece_id}")
@limiter.limit("30/minute")
async def edit_piece(
    request: Request,
    piece_id: str,
    body: EditPieceRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Edit piece content inline. Creates a new version automatically."""
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty.")

    updated = await update_piece_content(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        new_content=body.content.strip(),
        action="manual_edit",
        instruction="User edited content manually",
        actor_user_id=ctx.user_id,
    )
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
    return await get_piece(piece_id, ctx.workspace_id)


@router.patch("/pieces/{piece_id}/approve")
@limiter.limit("30/minute")
async def approve_piece(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Mark a piece as approved."""
    updated = await update_piece_status(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        approval_status="approved",
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
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

    platform = piece["platform"]
    slug = platform.lower()

    token_data = await get_token(ctx.workspace_id, slug)
    if not token_data:
        raise HTTPException(
            status_code=400,
            detail=f"{platform} is not connected. Connect it in Settings before scheduling.",
        )

    try:
        get_publisher(platform)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Publishing to {platform} isn't supported yet.",
        )

    is_valid, issues = validate_for_platform(platform, piece["content"])
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Content validation failed: {'; '.join(issues)}",
        )

    updated = await update_piece_status(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        publish_status="queued",
        publish_scheduled_at=body.scheduled_at,
        publish_target=slug,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
    return updated


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

    updated = await update_piece_status(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        publish_status="pending",
        publish_scheduled_at="",
        publish_target="",
    )
    return updated


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


@router.patch("/sessions/{session_id}/approve-all")
@limiter.limit("20/minute")
async def approve_all(
    request: Request,
    session_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Approve all pieces in a session at once."""
    count = await approve_all_pieces(session_id, ctx.workspace_id)
    if count == 0:
        raise HTTPException(status_code=404, detail="Session not found or no pieces.")
    return {"session_id": session_id, "approved_count": count}


@router.delete("/pieces/{piece_id}")
@limiter.limit("20/minute")
async def remove_piece(
    request: Request,
    piece_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Soft delete a piece."""
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
