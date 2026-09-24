"""Bridge from the text pipeline to the workspace event bus.

This is the ``pipeline_type=PipelineType.TEXT`` call site for
:func:`app.shared.events.emit_event`. Audio/Image/Video get their own identical
bridge module later — the agent layer never changes.

Emission is fire-and-forget and fully guarded: a bus hiccup can never fail a
generation request or a storage write.
"""

from __future__ import annotations

import asyncio
import logging

from app.db.mongo import workspace_members
from app.models.agent_events import (
    ContentEventPayload,
    ContentRef,
    EventType,
    PipelineRunCompletedPayload,
)
from app.models.text import TextPipelineResult
from app.shared.events import emit_event
from app.shared.pipeline_types import PipelineType

logger = logging.getLogger(__name__)


async def _actor_role(workspace_id: str, user_id: str) -> str:
    try:
        m = await workspace_members.find_one(
            {"workspace_id": workspace_id, "user_id": user_id}, {"role": 1}
        )
        return (m or {}).get("role", "") or ""
    except Exception:  # noqa: BLE001
        return ""


async def _emit_pieces_created(result: TextPipelineResult, piece_ids: list[str]) -> None:
    role = await _actor_role(result.workspace_id, result.user_id)
    for piece, piece_id in zip(result.pieces, piece_ids):
        platform = piece.platform.value if hasattr(piece.platform, "value") else str(piece.platform)
        payload = ContentEventPayload(
            content_id=piece_id,
            content_ref=ContentRef(collection="content_pieces", id=piece_id),
            content_text=piece.content or "",
            content_summary=(piece.content or "")[:400],
            target=platform,
            word_count=piece.word_count or 0,
            quality_passed=bool(piece.quality_passed),
            flagged_for_review=bool(piece.flagged_for_review),
            brand_id=result.brand_id,
            session_id=result.session_id,
        )
        await emit_event(
            event_type=EventType.CONTENT_CREATED,
            pipeline_type=PipelineType.TEXT,
            workspace_id=result.workspace_id,
            actor_user_id=result.user_id,
            actor_role=role,
            payload=payload,
            idempotency_key=f"content.created:{piece_id}",
        )


def emit_pieces_created(result: TextPipelineResult, piece_ids: list[str]) -> None:
    """Schedule ``content.created`` events for a saved pipeline result. Returns
    immediately; never raises."""
    if not result.workspace_id or not result.user_id or not piece_ids:
        return
    try:
        task = asyncio.create_task(_emit_pieces_created(result, piece_ids))
        task.add_done_callback(_log_task)
    except RuntimeError:
        # No running loop (called from sync context) — emit synchronously best-effort.
        logger.debug("emit_pieces_created: no running loop; skipping background emit")


def _log_task(task: "asyncio.Task") -> None:
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001
        logger.error("emit_pieces_created task failed: %s", exc)


async def emit_run_completed(
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    platforms: list[str],
    requested: int,
    duration_ms: int,
    brand_id: str = "",
    title: str = "",
    trigger: str = "manual",
) -> None:
    """One ``pipeline.run_completed`` per run — the Activity Log's run summary
    and the Control Tower's ETA baseline. ``requested`` is how many outputs
    the run was asked for; the shortfall is reported as ``failed``. Never
    raises."""
    if not workspace_id or not user_id or not session_id:
        return
    try:
        await emit_event(
            event_type=EventType.PIPELINE_RUN_COMPLETED,
            pipeline_type=PipelineType.TEXT,
            workspace_id=workspace_id,
            actor_user_id=user_id,
            actor_role=await _actor_role(workspace_id, user_id),
            payload=PipelineRunCompletedPayload(
                session_id=session_id,
                pieces=len(platforms),
                failed=max(requested - len(platforms), 0),
                duration_ms=max(duration_ms, 0),
                brand_id=brand_id,
                platforms=platforms,
                title=_run_title(title),
                trigger=trigger,
            ),
            idempotency_key=f"pipeline.run_completed:{session_id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("emit_run_completed failed for session %s: %s", session_id, exc)


def _run_title(text: str) -> str:
    """First line of the source, trimmed to a label."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return first if len(first) <= 80 else first[:77].rstrip() + "…"
