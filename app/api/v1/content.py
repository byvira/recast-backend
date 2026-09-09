"""
Content management endpoints — sessions, pieces, versions.
Sprint 4 — storage and content management.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.pipelines.text.storage import (
    get_session,
    get_user_sessions,
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """List all content sessions for the authenticated user."""
    return await get_user_sessions(
        user_id=current_user["id"],
        brand_id=brand_id,
        page=page,
        limit=limit,
    )


@router.get("/sessions/{session_id}")
@limiter.limit("60/minute")
async def get_session_detail(
    request: Request,
    session_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Fetch one session with all its pieces."""
    session = await get_session(session_id, current_user["id"])
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Fetch one piece by ID."""
    piece = await get_piece(piece_id, current_user["id"])
    if not piece:
        raise HTTPException(status_code=404, detail="Piece not found.")
    return piece


@router.patch("/pieces/{piece_id}")
@limiter.limit("30/minute")
async def edit_piece(
    request: Request,
    piece_id: str,
    body: EditPieceRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Edit piece content inline. Creates a new version automatically."""
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty.")

    updated = await update_piece_content(
        piece_id=piece_id,
        user_id=current_user["id"],
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Mark a piece as approved."""
    updated = await update_piece_status(
        piece_id=piece_id,
        user_id=current_user["id"],
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Mark a piece as rejected."""
    updated = await update_piece_status(
        piece_id=piece_id,
        user_id=current_user["id"],
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Set a scheduled publish time for a piece."""
    updated = await update_piece_status(
        piece_id=piece_id,
        user_id=current_user["id"],
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Approve all pieces in a session at once."""
    count = await approve_all_pieces(session_id, current_user["id"])
    if count == 0:
        raise HTTPException(status_code=404, detail="Session not found or no pieces.")
    return {"session_id": session_id, "approved_count": count}


@router.delete("/pieces/{piece_id}")
@limiter.limit("20/minute")
async def remove_piece(
    request: Request,
    piece_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Soft delete a piece."""
    deleted = await delete_piece(piece_id, current_user["id"])
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """List all versions of a piece ordered by version number."""
    versions = await get_versions(piece_id, current_user["id"])
    if not versions:
        piece = await get_piece(piece_id, current_user["id"])
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
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict:
    """Restore a piece to a specific version. Creates a new version entry."""
    restored = await restore_version(
        piece_id=piece_id,
        user_id=current_user["id"],
        version_number=version_number,
    )
    if not restored:
        raise HTTPException(
            status_code=404,
            detail=f"Piece or version {version_number} not found.",
        )
    return restored