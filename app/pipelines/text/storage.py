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
from typing import Any, Optional

from app.db.mongo import content_sessions, content_pieces, content_piece_versions, users, brand_profiles
from app.pipelines.publish.spine import normalize_piece_dates
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
# LIVE (SSE) PERSISTENCE — one piece at a time, as each platform finishes
# ─────────────────────────────────────────────────────────────────────────────
#
# save_pipeline_result() below saves an entire TextPipelineResult in one go,
# which only works for the blocking /generate, /repurpose and /batch routes
# that await the whole pipeline before saving anything. The SSE route
# (GET /api/v1/pipeline/generate/stream) streams each platform's
# output_complete event the moment that platform's own graph run finishes —
# platforms run in parallel, so there is no single "whole result" to save
# until every platform is done, and output_complete needs a real piece_id
# immediately, not after the fact. ensure_session_exists() and
# save_live_piece() below let each platform persist itself independently,
# the instant it finishes, so the piece_id handed back in that platform's
# own output_complete event is real and immediately usable by
# approve/refine/rescore/versions — not the empty string every SSE-driven
# card carried before this.

MAX_INPUT_TEXT = 8000


async def ensure_session_exists(
    session_id: str,
    workspace_id: str,
    user_id: str,
    brand_id: str,
    source_type: str,
    goal: Optional[str] = None,
    tone: Optional[str] = None,
    is_repurpose: bool = False,
    batch_mode: bool = False,
    schedule_mode: str = "now",
    scheduled_at: Optional[str] = None,
    input_text: Optional[str] = None,
    extras: Optional[dict] = None,
) -> None:
    """Idempotent — safe to call once per platform. Platforms for one
    generation run finish concurrently and each calls this before saving its
    own piece; the upsert with $setOnInsert means whichever platform gets
    there first creates the session document and every later call is just a
    no-op touch of updated_at, so there's no race to coordinate explicitly.
    """
    now = datetime.now(timezone.utc)
    await content_sessions.update_one(
        {"session_id": session_id},
        {
            "$setOnInsert": {
                "session_id": session_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "brand_id": brand_id,
                "source_type": source_type,
                "platforms": [],
                "goal": goal,
                "tone": tone,
                "batch_mode": batch_mode,
                "is_repurpose": is_repurpose,
                "schedule_mode": schedule_mode or "now",
                "scheduled_at": str(scheduled_at) if scheduled_at else None,
                "pieces_count": 0,
                # What the member typed or pasted, kept (capped) so History can open this work again with the input filled in
                "input_text": (input_text or "")[:MAX_INPUT_TEXT],
                # The Extras toggles the run used, so a regenerate can repeat the run instead of using fixed defaults.
                "extras": extras or None,
                "created_at": now,
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
    )


async def save_live_piece(
    session_id: str,
    workspace_id: str,
    user_id: str,
    brand_id: str,
    platform: str,
    content: str,
    word_count: int,
    char_count: int,
    hooks: Optional[list[dict]] = None,
    seo: Optional[dict] = None,
    quality_passed: bool = True,
    quality_issues: Optional[list[str]] = None,
    flagged_for_review: bool = False,
    readability_score: Optional[float] = None,
    repurposed: bool = False,
    publish_status: Optional[str] = None,
    publish_scheduled_at=None,
    publish_target: Optional[str] = None,
    sections: Optional[list[dict]] = None,
    source_platform: Optional[str] = None,
    intended_publish_at: Optional[datetime] = None,
    piece_id: Optional[str] = None,
    extra_fields: Optional[dict] = None,
) -> str:
    """Persist one freshly-generated piece the moment its own graph run
    finishes. Mirrors save_pipeline_result's piece/version-1 document shape
    exactly, so a piece created this way is indistinguishable to every
    downstream reader (approve, refine, versions, Drafts/Library) from one
    created by the blocking /generate route. Requires ensure_session_exists()
    to have been called first for this session_id. Returns the real piece_id.
    """
    now = datetime.now(timezone.utc)
    # A caller that needs a repeatable id (send-to-draft) passes its own; the
    # unique index on piece_id then makes a second insert fail instead of duplicating.
    piece_id = piece_id or str(uuid4())

    piece_doc = {
        "piece_id": piece_id,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "brand_id": brand_id,
        "platform": platform,
        "source_platform": source_platform,
        "content": content,
        "sections": sections,
        "word_count": word_count,
        "char_count": char_count,
        "hooks": hooks or [],
        "seo": seo or {},
        "quality_passed": quality_passed,
        "quality_issues": quality_issues or [],
        "flagged_for_review": flagged_for_review,
        "readability_score": readability_score,
        "approval_status": ApprovalStatus.PENDING.value,
        "repurposed": repurposed,
        "publish_status": publish_status or PublishStatus.PENDING.value,
        "publish_scheduled_at": publish_scheduled_at,
        # Planned time from generation: intent only. It becomes a real queued
        # post when the piece is approved (see spine.promote_approved_intent).
        "intended_publish_at": intended_publish_at,
        "publish_target": publish_target,
        "publish_job_id": None,
        "attachments": [],
        "version_count": 1,
        "deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    piece_doc.update(extra_fields or {})
    await content_pieces.insert_one(piece_doc)
    try:
        from app.pipelines.text.events import emit_live_piece_created
        emit_live_piece_created(piece_doc)
    except Exception as exc:  # noqa: BLE001
        logger.error("save_live_piece: event emit scheduling failed: %s", exc)

    version_doc = {
        "version_id": str(uuid4()),
        "piece_id": piece_id,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "version_number": 1,
        "content": content,
        "word_count": word_count,
        "char_count": char_count,
        "action": "original",
        "instruction": "Initial generation",
        "platform": platform,
        "created_at": now,
    }
    await content_piece_versions.insert_one(version_doc)

    await content_sessions.update_one(
        {"session_id": session_id},
        {
            "$addToSet": {"platforms": platform},
            "$inc": {"pieces_count": 1},
            "$set": {"updated_at": now},
        },
    )

    return piece_id


# ─────────────────────────────────────────────────────────────────────────────
# SAVE PIPELINE RESULT
# ─────────────────────────────────────────────────────────────────────────────

async def _adopt_saved_piece(piece, result, campaign_id, workspace_id: str, now) -> Optional[str]:
    """The piece_id when `piece` was already saved by its own graph run (and is now brought under this result's session),
    otherwise None so the caller saves it as new."""
    saved_id = getattr(piece, "piece_id", None)
    if not saved_id:
        return None
    existing = await content_pieces.find_one({"piece_id": saved_id, "workspace_id": workspace_id}, {"session_id": 1})
    if not existing:
        return None
    updates = {
        "session_id": result.session_id,
        "batch_day_index": result.batch_day_index,
        "angle": result.angle,
        "source_platform": result.source_platform,
        "updated_at": now,
    }
    if campaign_id:
        updates["campaign_id"] = campaign_id
    await content_pieces.update_one({"piece_id": saved_id, "workspace_id": workspace_id}, {"$set": updates})
    old_session = existing.get("session_id")
    if old_session and old_session != result.session_id:
        await content_piece_versions.update_many({"piece_id": saved_id, "workspace_id": workspace_id}, {"$set": {"session_id": result.session_id}})
        await _release_session(old_session, workspace_id)
    return saved_id


async def _release_session(session_id: str, workspace_id: str) -> None:
    """After a piece moves out of a session: drop the session if nothing is left in it, else fix its count."""
    left = await content_pieces.count_documents({"session_id": session_id, "workspace_id": workspace_id})
    if left == 0:
        await content_sessions.delete_one({"session_id": session_id, "workspace_id": workspace_id})
    else:
        await content_sessions.update_one({"session_id": session_id, "workspace_id": workspace_id}, {"$set": {"pieces_count": left}})


async def discard_generated_piece(piece_id: str, workspace_id: str) -> None:
    """Remove a piece a run saved that is not wanted (for example the copy a regenerate made when the new text was put
    on the existing piece as a new version instead). Its versions go with it, and its session if that is now empty."""
    doc = await content_pieces.find_one({"piece_id": piece_id, "workspace_id": workspace_id}, {"session_id": 1})
    if not doc:
        return
    await content_piece_versions.delete_many({"piece_id": piece_id, "workspace_id": workspace_id})
    await content_pieces.delete_one({"piece_id": piece_id, "workspace_id": workspace_id})
    if doc.get("session_id"):
        await _release_session(doc["session_id"], workspace_id)


async def save_pipeline_result(
    result: TextPipelineResult,
    goal: Optional[str] = None,
    tone: Optional[str] = None,
    is_repurpose: bool = False,
    campaign_id: Optional[str] = None,
) -> tuple[str, list[str]]:
    """
    Save a complete pipeline result to MongoDB.
    Creates one ContentSession and one ContentPiece per platform.
    Creates Version 1 (original) for every piece automatically.

    ``result.workspace_id`` is the scope; ``result.user_id`` is the creator.
    ``campaign_id`` tags every piece for a campaign's generate-next-batch
    run (mirrors how session_id already links pieces) — None for every
    other caller, unchanged from before this param existed.

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
        "batch_day_index": result.batch_day_index,
        "is_repurpose": is_repurpose,
        "schedule_mode": result.schedule_mode or "now",
        "scheduled_at": str(result.scheduled_at) if result.scheduled_at else None,
        "pieces_count": len(result.pieces),
        "created_at": now,
        "updated_at": now,
    }

    # An upsert: the run's own graph may already have created this session. Insert-only used to fail on the unique index
    # or leave a second session behind.
    await content_sessions.update_one(
        {"session_id": result.session_id},
        {"$setOnInsert": {k: v for k, v in session_doc.items() if k != "updated_at"}, "$set": {"updated_at": now}},
        upsert=True,
    )
    logger.info("Session saved: %s (%d pieces)", result.session_id, len(result.pieces))

    # ── Save each piece + version 1 ───────────────────────────────────────
    for piece in result.pieces:
        # The graph already saved this piece the moment it finished (it carries that piece_id). Saving again made a
        # second copy of every piece in batch, campaign and blocking runs, so adopt the saved one instead: bring it
        # under this session and add what only this caller knows (campaign, batch day, angle).
        adopted = await _adopt_saved_piece(piece, result, campaign_id, workspace_id, now)
        if adopted:
            piece_ids.append(adopted)
            continue

        piece_id = str(uuid4())
        piece_ids.append(piece_id)

        platform_value = piece.platform.value if hasattr(piece.platform, "value") else str(piece.platform)

        piece_doc = {
            "piece_id": piece_id,
            "session_id": result.session_id,
            "workspace_id": workspace_id,
            "user_id": result.user_id,        # creator (audit)
            "brand_id": result.brand_id,
            "campaign_id": campaign_id,
            "batch_day_index": result.batch_day_index,
            "angle": result.angle,
            "platform": platform_value,
            "source_platform": result.source_platform,
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
            "intended_publish_at": getattr(piece, "intended_publish_at", None),
            "publish_target": piece.publish_target,
            "publish_job_id": None,
            "attachments": [],
            "version_count": 1,
            "deleted": False,
            "created_at": now,
            "updated_at": now,
        }
        # The default picture chosen for the run, so a piece saved here keeps it (it used to be dropped on this path).
        if getattr(piece, "media", None):
            piece_doc["media"] = [m.model_dump(mode="json") if hasattr(m, "model_dump") else m for m in piece.media]

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
        normalize_piece_dates(piece)

    return session


async def get_workspace_sessions(
    workspace_id: str,
    brand_id: Optional[str] = None,
    is_repurpose: Optional[bool] = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """
    Paginated list of sessions for a workspace.
    Optionally filter by brand_id and/or is_repurpose.
    Returns sessions without pieces — use get_session() for full detail.
    """
    query: dict = {"workspace_id": workspace_id}
    if brand_id:
        query["brand_id"] = brand_id
    if is_repurpose is not None:
        query["is_repurpose"] = is_repurpose

    skip = (page - 1) * limit
    total = await content_sessions.count_documents(query)

    sessions = await content_sessions.find(query).sort(
        "created_at", -1
    ).skip(skip).limit(limit).to_list(length=limit)

    for s in sessions:
        s.pop("_id", None)
        # the list carries only a short preview; the full input comes with the single session
        s["preview"] = " ".join((s.pop("input_text", "") or "").split())[:160]

    return {
        "items": sessions,
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": (skip + limit) < total,
    }


KANBAN_STAGES = ("drafting", "staging", "scheduled", "published", "failed", "archived")

# publish_status values that mean "queued to actually go out" — the real
# worker-recognized "queued" plus its transient "publishing" follow-on.
# "scheduled" is a dead legacy value: two write paths used to emit it
# instead of "queued" (neither the real Module 2 Stage 9 fix), so it's kept
# here only so pieces written before that fix still land in the right
# kanban column rather than silently vanishing into "drafting".
_QUEUED_LIKE = ("queued", "publishing", "scheduled")


def compute_kanban_stage(piece: dict) -> str:
    """Derive the Drafts/Library kanban stage from real status fields.

    Drafts preserves the mock UI's 4-step workflow (drafting -> staging ->
    scheduled -> published, plus a lateral archive) even though nothing in
    the real data model stores a "stage" — only ``approval_status``
    (pending/approved/rejected) and ``publish_status``
    (pending/queued/publishing/published/failed) plus the ``archived`` flag
    exist. Computing it on read instead of storing it avoids a second,
    driftable source of truth.
    """
    if piece.get("archived"):
        return "archived"
    publish_status = piece.get("publish_status")
    if publish_status == "published":
        return "published"
    if publish_status == "failed":
        return "failed"
    if publish_status in _QUEUED_LIKE:
        return "scheduled"
    if piece.get("approval_status") == "approved":
        return "staging"
    return "drafting"


def _stage_query(stage: str) -> dict:
    """Translate a kanban stage filter into the real-field Mongo query that
    produces it, so pagination/counts stay correct (computing in Python
    after the DB skip/limit would paginate over the wrong set)."""
    if stage == "archived":
        return {"archived": True}
    base = {"archived": {"$ne": True}}
    if stage == "published":
        return {**base, "publish_status": "published"}
    if stage == "failed":
        return {**base, "publish_status": "failed"}
    if stage == "scheduled":
        return {**base, "publish_status": {"$in": list(_QUEUED_LIKE)}}
    not_queued_or_terminal = {"$nin": [*_QUEUED_LIKE, "published", "failed"]}
    if stage == "staging":
        return {**base, "publish_status": not_queued_or_terminal, "approval_status": "approved"}
    if stage == "drafting":
        return {
            **base,
            "publish_status": not_queued_or_terminal,
            "approval_status": {"$ne": "approved"},
        }
    return {}


async def _attach_display_names(pieces: list[dict]) -> None:
    """Batch-resolve author (user) and brand display names onto each piece
    dict in place. Two bulk lookups regardless of list size, not N+1."""
    user_ids = {p.get("user_id") for p in pieces if p.get("user_id")}
    brand_ids = {p.get("brand_id") for p in pieces if p.get("brand_id")}

    user_names: dict[str, str] = {}
    if user_ids:
        async for u in users.find({"id": {"$in": list(user_ids)}}, {"id": 1, "name": 1}):
            user_names[u["id"]] = u.get("name") or "Unknown"

    brand_names: dict[str, str] = {}
    if brand_ids:
        async for b in brand_profiles.find({"id": {"$in": list(brand_ids)}}, {"id": 1, "identity": 1}):
            identity = b.get("identity") or {}
            brand_names[b["id"]] = (
                identity.get("name")
                or identity.get("productName")
                or identity.get("company_name")
                or identity.get("companyName")
                or "Untitled Brand"
            )

    for p in pieces:
        p["author_name"] = user_names.get(p.get("user_id", ""), "Unknown")
        p["brand_name"] = brand_names.get(p.get("brand_id", ""), "Untitled Brand")


async def get_workspace_pieces(
    workspace_id: str,
    page: int = 1,
    limit: int = 20,
    platform: Optional[str] = None,
    approval_status: Optional[str] = None,
    brand_id: Optional[str] = None,
    stage: Optional[str] = None,
    campaign_id: Optional[str] = None,
) -> dict:
    """
    Paginated, flat list of pieces across every session in the workspace,
    most recent first — deliberately not grouped by session. Powers
    Drafts and Library (Module 2 Stage 8): both need the real piece
    history regardless of session, not the session-then-pieces shape
    get_session()/get_workspace_sessions() return. ``campaign_id`` powers
    the real Pipeline page's per-campaign branch view.
    """
    query: dict = {"workspace_id": workspace_id, "deleted": {"$ne": True}}
    if platform:
        query["platform"] = platform
    if approval_status:
        query["approval_status"] = approval_status
    if brand_id:
        query["brand_id"] = brand_id
    if campaign_id:
        query["campaign_id"] = campaign_id
    if stage and stage in KANBAN_STAGES:
        query.update(_stage_query(stage))

    skip = (page - 1) * limit
    total = await content_pieces.count_documents(query)

    pieces = await content_pieces.find(query).sort(
        "created_at", -1
    ).skip(skip).limit(limit).to_list(length=limit)

    for p in pieces:
        p.pop("_id", None)
        p["stage"] = compute_kanban_stage(p)
        normalize_piece_dates(p)

    await _attach_display_names(pieces)

    return {
        "items": pieces,
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": (skip + limit) < total,
    }


async def get_all_workspace_pieces(
    workspace_id: str,
    platform: Optional[str] = None,
    brand_id: Optional[str] = None,
) -> list[dict]:
    """Every real piece in the workspace matching the given filters, no
    pagination — real export (Library's Markdown/CSV/ZIP buttons) needs
    the caller's *entire* library, not one page of it, and needs a fresh
    DB read at export time rather than whatever a page's already-loaded
    React Query cache happens to hold (added 2026-09-26, standalone-usage
    audit — see pow/audio_image_pipeline/GAPS.md). Same query shape as
    get_workspace_pieces, just without skip/limit."""
    query: dict = {"workspace_id": workspace_id, "deleted": {"$ne": True}}
    if platform:
        query["platform"] = platform
    if brand_id:
        query["brand_id"] = brand_id

    pieces = await content_pieces.find(query).sort("created_at", -1).to_list(length=None)
    for p in pieces:
        p.pop("_id", None)
        p["stage"] = compute_kanban_stage(p)
        normalize_piece_dates(p)
    await _attach_display_names(pieces)
    return pieces


async def get_piece(piece_id: str, workspace_id: str) -> Optional[dict]:
    """Fetch one piece by piece_id. Returns None if not found or outside the workspace."""
    piece = await content_pieces.find_one(
        {"piece_id": piece_id, "deleted": {"$ne": True}}
    )
    if not piece or piece.get("workspace_id") != workspace_id:
        return None
    piece.pop("_id", None)
    piece["stage"] = compute_kanban_stage(piece)
    return normalize_piece_dates(piece)


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────────────────────

_DIFF_CHARS = 1200


def _edit_title(action: str, platform: str) -> str:
    if action == "manual_edit":
        return f"Edited {platform} draft"
    if action == "regenerated":
        return f"Regenerated {platform} draft"
    if action.startswith("restored_from_v"):
        return f"Restored {platform} draft to version {action.removeprefix('restored_from_v')}"
    if action.startswith("chat_turn_"):
        return f"Refined {platform} draft in chat"
    return f"Refined {platform} draft ({action.replace('_', ' ')})"


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= _DIFF_CHARS else text[:_DIFF_CHARS].rstrip() + "…"


async def _record_edit(
    *, piece: dict, new_content: str, previous_version: int, action: str, actor_user_id: str,
) -> None:
    """Passive-lane row with the diff; ``restore`` lets the Activity Log's
    "Restore this version" put the pre-edit version back."""
    from app.shared.activity import record_system
    platform = piece.get("platform", "")
    await record_system(
        workspace_id=piece["workspace_id"],
        key=f"edit:{piece['piece_id']}:{previous_version + 1}",
        actor_name="",
        actor_user_id=actor_user_id,
        category="content_edited",
        title=_edit_title(action, platform),
        description=f"Saved as version {previous_version + 1}.",
        channel=platform,
        target_id=piece["piece_id"],
        target_type="Draft Post",
        href="/dashboard/drafts",
        diff={"field": "Content", "before": _clip(piece.get("content", "")), "after": _clip(new_content)},
        restore={"piece_id": piece["piece_id"], "version_number": previous_version},
    )


async def update_piece_content(
    piece_id: str,
    workspace_id: str,
    new_content: str,
    action: str,
    instruction: str,
    actor_user_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Update piece content after chip or chat refinement.
    Automatically creates a new version and increments version_count.
    Returns updated piece or None if not found.

    ``actor_user_id`` (the member making the change) records the edit in the
    Activity Log with a before/after diff and the version to restore.
    """
    from app.pipelines.text.quality import flesch_reading_ease

    piece = await get_piece(piece_id, workspace_id)
    if not piece:
        return None

    now = datetime.now(timezone.utc)
    new_word_count = len(new_content.split())
    new_char_count = len(new_content)
    # readability_score used to just go stale here — this is the only write
    # path for manual edits, chip/chat refinement, version restore, and
    # regenerate-with-an-existing-piece_id, none of which ever recomputed
    # it, so the stored score kept describing whatever content the piece
    # had *before* this edit (or stayed permanently null if the piece
    # started out non-Latin-script and the edit changed that). Recomputing
    # here — the same flesch_reading_ease() the initial generation quality
    # gate uses — closes every one of those gaps in the one place they all
    # funnel through, instead of fixing each caller separately.
    new_readability_score = flesch_reading_ease(new_content)

    # Update the piece and take the next version number in ONE step. Reading the count and writing count + 1 later let two
    # edits that overlap (autosave, a chip and a manual edit) both claim the same number.
    from pymongo import ReturnDocument
    bumped = await content_pieces.find_one_and_update(
        {"piece_id": piece_id, "workspace_id": workspace_id},
        {
            "$set": {
                "content": new_content,
                "word_count": new_word_count,
                "char_count": new_char_count,
                "readability_score": new_readability_score,
                "updated_at": now,
            },
            "$inc": {"version_count": 1},
        },
        projection={"version_count": 1},
        return_document=ReturnDocument.AFTER,
    )
    if not bumped:
        return None
    new_version_number = bumped.get("version_count", 2)

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

    if actor_user_id:
        await _record_edit(
            piece=piece, new_content=new_content, previous_version=new_version_number - 1,
            action=action, actor_user_id=actor_user_id,
        )

    return await get_piece(piece_id, workspace_id)


async def update_piece_status(
    piece_id: str,
    workspace_id: str,
    approval_status: Optional[str] = None,
    publish_status: Optional[str] = None,
    publish_scheduled_at: Optional[Any] = None,
    publish_target: Optional[str] = None,
    archived: Optional[bool] = None,
) -> Optional[dict]:
    """Update approval, publish status, and/or archive flag of a piece."""
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
    if publish_target is not None:
        updates["publish_target"] = publish_target
    if archived is not None:
        updates["archived"] = archived

    await content_pieces.update_one(
        {"piece_id": piece_id, "workspace_id": workspace_id}, {"$set": updates}
    )
    if approval_status:
        # Shadow-mode trust score: log what it would have done vs. this human call.
        from app.agents.feedback.trust import record_shadow
        await record_shadow(piece, approval_status)
    return await get_piece(piece_id, workspace_id)


async def approve_all_pieces(session_id: str, workspace_id: str) -> int:
    """
    Approve all pieces in a session.
    Returns count of pieces approved.
    """
    session = await content_sessions.find_one({"session_id": session_id})
    if not session or session.get("workspace_id") != workspace_id:
        return 0

    session_filter = {
        "session_id": session_id,
        "workspace_id": workspace_id,
        "deleted": {"$ne": True},
    }
    # Pre-change state for the trust score's shadow log (one session = a
    # handful of pieces, so this read is small).
    before = await content_pieces.find(
        {**session_filter, "approval_status": {"$ne": ApprovalStatus.APPROVED.value}},
        {"piece_id": 1, "workspace_id": 1, "platform": 1, "pipeline_type": 1,
         "approval_status": 1, "version_count": 1},
    ).to_list(length=200)

    result = await content_pieces.update_many(
        session_filter,
        {"$set": {
            "approval_status": ApprovalStatus.APPROVED.value,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    from app.agents.feedback.trust import record_shadow
    for piece in before:
        await record_shadow(piece, ApprovalStatus.APPROVED.value)
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
    actor_user_id: Optional[str] = None,
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
        actor_user_id=actor_user_id,
    )
