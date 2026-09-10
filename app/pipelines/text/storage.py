"""
Content storage — save and retrieve generated content sessions and pieces.

Called by API routes after generation completes.
Every piece gets a piece_id. Every session gets a session_id.
Version 1 is created automatically when a piece is first saved.

All ownership is scoped by ``workspace_id``. ``user_id`` is retained on each
document as the creator (audit / created_by) and is not used for access control.

Functions:
  save_pipeline_result()      save a complete TextPipelineResult to MongoDB
  get_session()               fetch one session with all its pieces
  get_workspace_sessions()    paginated list of sessions for a workspace
  get_piece()                 fetch one piece by piece_id
  update_piece_content()      update content after chip/chat refinement
  update_piece_status()       approve, reject
  delete_piece()              soft delete — sets deleted: True
  get_versions()              list all versions of a piece
  restore_version()           set a version as current content
"""

import logging
from datetime import datetime, timezone
from uuid import uuid4
from typing import Optional

from app.db.mongo import content_sessions, content_pieces, content_piece_versions
from app.models.text import (
    ContentSession,
    ContentPiece,
    ContentPieceVersion,
    TextPipelineResult,
    ApprovalStatus,
    PublishStatus,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SAVE PIPELINE RESULT
# ─────────────────────────────────────────────────────────────────────────────

async def save_pipeline_result(
    result: TextPipelineResult,
    goal: Optional[str] = None,
    tone: Optional[str] = None,
    is_repurpose: bool = False,
) -> tuple[str, list[str]]:
    """
    Save a complete pipeline result to MongoDB.
    Creates one ContentSession and one ContentPiece per platform.
    Creates Version 1 (original) for every piece automatically.

    ``result.workspace_id`` is the scope; ``result.user_id`` is the creator.

    Returns:
        (session_id, list of piece_ids)
    """
    now = datetime.now(timezone.utc)
    piece_ids = []
    workspace_id = result.workspace_id

    # ── Save session ──────────────────────────────────────────────────────
    session_doc = {
        "session_id": result.session_id,
        "workspace_id": workspace_id,
        "user_id": result.user_id,           # creator (audit)
        "brand_id": result.brand_id,
        "source_type": result.source_type.value if hasattr(result.source_type, "value") else str(result.source_type),
        "platforms": [p.platform.value if hasattr(p.platform, "value") else str(p.platform) for p in result.pieces],
        "goal": goal,
        "tone": tone,
        "batch_mode": result.batch_mode,
        "is_repurpose": is_repurpose,
        "schedule_mode": result.schedule_mode or "now",
        "scheduled_at": str(result.scheduled_at) if result.scheduled_at else None,
        "pieces_count": len(result.pieces),
        "created_at": now,
        "updated_at": now,
    }

    await content_sessions.insert_one(session_doc)
    logger.info("Session saved: %s (%d pieces)", result.session_id, len(result.pieces))

    # ── Save each piece + version 1 ───────────────────────────────────────
    for piece in result.pieces:
        piece_id = str(uuid4())
        piece_ids.append(piece_id)

        platform_value = piece.platform.value if hasattr(piece.platform, "value") else str(piece.platform)

        piece_doc = {
            "piece_id": piece_id,
            "session_id": result.session_id,
            "workspace_id": workspace_id,
            "user_id": result.user_id,        # creator (audit)
            "brand_id": result.brand_id,
            "platform": platform_value,
            "content": piece.content,
            "word_count": piece.word_count,
            "char_count": piece.char_count,
            "hooks": piece.hooks,
            "seo": piece.seo,
            "quality_passed": piece.quality_passed,
            "quality_issues": piece.quality_issues,
            "flagged_for_review": piece.flagged_for_review,
            "readability_score": getattr(piece, "readability_score", None),
            "approval_status": ApprovalStatus.PENDING.value,
            "repurposed": piece.repurposed,
            "publish_status": PublishStatus.PENDING.value,
            "publish_scheduled_at": piece.publish_scheduled_at,
            "publish_target": piece.publish_target,
            "publish_job_id": None,
            "version_count": 1,
            "deleted": False,
            "created_at": now,
            "updated_at": now,
        }

        await content_pieces.insert_one(piece_doc)

        # Version 1 — original generated content
        version_doc = {
            "version_id": str(uuid4()),
            "piece_id": piece_id,
            "session_id": result.session_id,
            "workspace_id": workspace_id,
            "user_id": result.user_id,        # creator (audit)
            "version_number": 1,
            "content": piece.content,
            "word_count": piece.word_count,
            "char_count": piece.char_count,
            "action": "original",
            "instruction": "Initial generation",
            "platform": platform_value,
            "created_at": now,
        }

        await content_piece_versions.insert_one(version_doc)

    logger.info(
        "Saved %d pieces for session %s",
        len(result.pieces), result.session_id,
    )

    # Fire content.created onto the workspace event bus (Layer-1 personal
    # assistant consumes these). Fire-and-forget, never raises, no latency.
    try:
        from app.pipelines.text.events import emit_pieces_created
        emit_pieces_created(result, piece_ids)
    except Exception as exc:  # noqa: BLE001
        logger.error("save_pipeline_result: event emit scheduling failed: %s", exc)

    return result.session_id, piece_ids


# ─────────────────────────────────────────────────────────────────────────────
# READ
# ─────────────────────────────────────────────────────────────────────────────

async def get_session(session_id: str, workspace_id: str) -> Optional[dict]:
    """
    Fetch one session with all its pieces.
    Returns None if not found or outside the workspace.
    """
    session = await content_sessions.find_one({"session_id": session_id})
    if not session or session.get("workspace_id") != workspace_id:
        return None

    pieces = await content_pieces.find(
        {"session_id": session_id, "deleted": {"$ne": True}}
    ).sort("created_at", 1).to_list(length=100)

    session["pieces"] = pieces
    session.pop("_id", None)
    for piece in pieces:
        piece.pop("_id", None)

    return session


async def get_workspace_sessions(
    workspace_id: str,
    brand_id: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """
    Paginated list of sessions for a workspace.
    Optionally filter by brand_id.
    Returns sessions without pieces — use get_session() for full detail.
    """
    query: dict = {"workspace_id": workspace_id}
    if brand_id:
        query["brand_id"] = brand_id

    skip = (page - 1) * limit
    total = await content_sessions.count_documents(query)

    sessions = await content_sessions.find(query).sort(
        "created_at", -1
    ).skip(skip).limit(limit).to_list(length=limit)

    for s in sessions:
        s.pop("_id", None)

    return {
        "items": sessions,
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": (skip + limit) < total,
    }


async def get_piece(piece_id: str, workspace_id: str) -> Optional[dict]:
    """Fetch one piece by piece_id. Returns None if not found or outside the workspace."""
    piece = await content_pieces.find_one(
        {"piece_id": piece_id, "deleted": {"$ne": True}}
    )
    if not piece or piece.get("workspace_id") != workspace_id:
        return None
    piece.pop("_id", None)
    return piece


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────────────────────

async def update_piece_content(
    piece_id: str,
    workspace_id: str,
    new_content: str,
    action: str,
    instruction: str,
) -> Optional[dict]:
    """
    Update piece content after chip or chat refinement.
    Automatically creates a new version and increments version_count.
    Returns updated piece or None if not found.
    """
    piece = await get_piece(piece_id, workspace_id)
    if not piece:
        return None

    now = datetime.now(timezone.utc)
    new_version_number = piece.get("version_count", 1) + 1
    new_word_count = len(new_content.split())
    new_char_count = len(new_content)

    # Update piece
    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": workspace_id},
        {"$set": {
            "content": new_content,
            "word_count": new_word_count,
            "char_count": new_char_count,
            "version_count": new_version_number,
            "updated_at": now,
        }},
    )

    # Save new version
    version_doc = {
        "version_id": str(uuid4()),
        "piece_id": piece_id,
        "session_id": piece["session_id"],
        "workspace_id": workspace_id,
        "user_id": piece.get("user_id", ""),      # carry the piece's creator
        "version_number": new_version_number,
        "content": new_content,
        "word_count": new_word_count,
        "char_count": new_char_count,
        "action": action,
        "instruction": instruction,
        "platform": piece["platform"],
        "created_at": now,
    }
    await content_piece_versions.insert_one(version_doc)

    logger.info(
        "Piece %s updated to v%d via %s",
        piece_id, new_version_number, action,
    )

    return await get_piece(piece_id, workspace_id)


async def update_piece_status(
    piece_id: str,
    workspace_id: str,
    approval_status: Optional[str] = None,
    publish_status: Optional[str] = None,
    publish_scheduled_at: Optional[str] = None,
) -> Optional[dict]:
    """Update approval or publish status of a piece."""
    piece = await get_piece(piece_id, workspace_id)
    if not piece:
        return None

    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if approval_status:
        updates["approval_status"] = approval_status
    if publish_status:
        updates["publish_status"] = publish_status
    if publish_scheduled_at is not None:
        updates["publish_scheduled_at"] = publish_scheduled_at

    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": workspace_id}, {"$set": updates}
    )
    return await get_piece(piece_id, workspace_id)


async def approve_all_pieces(session_id: str, workspace_id: str) -> int:
    """
    Approve all pieces in a session.
    Returns count of pieces approved.
    """
    session = await content_sessions.find_one({"session_id": session_id})
    if not session or session.get("workspace_id") != workspace_id:
        return 0

    result = await content_pieces.update_many(
        {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "deleted": {"$ne": True},
        },
        {"$set": {
            "approval_status": ApprovalStatus.APPROVED.value,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return result.modified_count


async def delete_piece(piece_id: str, workspace_id: str) -> bool:
    """
    Soft delete a piece — sets deleted: True.
    Returns True if deleted, False if not found.
    """
    piece = await get_piece(piece_id, workspace_id)
    if not piece:
        return False

    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": workspace_id},
        {"$set": {
            "deleted": True,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return True


# ─────────────────────────────────────────────────────────────────────────────
# VERSION HISTORY
# ─────────────────────────────────────────────────────────────────────────────

async def get_versions(piece_id: str, workspace_id: str) -> list[dict]:
    """
    List all versions of a piece ordered by version_number ascending.
    Returns empty list if piece not found or outside the workspace.
    """
    piece = await content_pieces.find_one({"piece_id": piece_id})
    if not piece or piece.get("workspace_id") != workspace_id:
        return []

    versions = await content_piece_versions.find(
        {"piece_id": piece_id}
    ).sort("version_number", 1).to_list(length=100)

    for v in versions:
        v.pop("_id", None)

    return versions


async def restore_version(
    piece_id: str,
    workspace_id: str,
    version_number: int,
) -> Optional[dict]:
    """
    Restore a piece to a specific version.
    Creates a new version entry with action "restored_from_vN".
    Returns updated piece or None if not found.
    """
    piece = await get_piece(piece_id, workspace_id)
    if not piece:
        return None

    target_version = await content_piece_versions.find_one({
        "piece_id": piece_id,
        "version_number": version_number,
    })
    if not target_version:
        return None

    return await update_piece_content(
        piece_id=piece_id,
        workspace_id=workspace_id,
        new_content=target_version["content"],
        action=f"restored_from_v{version_number}",
        instruction=f"Restored to version {version_number}",
    )
