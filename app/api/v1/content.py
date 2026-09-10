"""
Content management endpoints — sessions, pieces, versions.

Workspace-scoped. Reads require workspace membership; edits/deletes/restores
require ``edit_content``; approve / reject / schedule / approve-all require
``approve_content`` (owner or admin only).
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.pipelines.text.storage import (
    get_session,
    get_workspace_sessions,
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


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST / RESPONSE MODELS
# ─────────────────────────────────────────────────────────────────────────────

class EditPieceRequest(BaseModel):
    content: str


class ApprovePieceRequest(BaseModel):
    approval_status: str  # "approved" or "rejected"


class SchedulePieceRequest(BaseModel):
    scheduled_at: str     # ISO datetime string


# ─────────────────────────────────────────────────────────────────────────────
# SESSION ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sessions")
@limiter.limit("60/minute")
async def list_sessions(
    request: Request,
    brand_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """List all content sessions in the active workspace."""
    return await get_workspace_sessions(
        workspace_id=ctx.workspace_id,
        brand_id=brand_id,
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
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Piece not found.")
    return updated


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
    return updated


@router.patch("/pieces/{piece_id}/schedule")
@limiter.limit("30/minute")
async def schedule_piece(
    request: Request,
    piece_id: str,
    body: SchedulePieceRequest,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Set a scheduled publish time for a piece."""
    updated = await update_piece_status(
        piece_id=piece_id,
        workspace_id=ctx.workspace_id,
        publish_status="scheduled",
        publish_scheduled_at=body.scheduled_at,
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
    )
    if not restored:
        raise HTTPException(
            status_code=404,
            detail=f"Piece or version {version_number} not found.",
        )
    return restored
