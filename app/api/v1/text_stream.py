"""
app/api/v1/text_stream.py

SSE streaming endpoint for the text pipeline.
GET /api/v1/pipeline/generate/stream

EventSource only supports GET — payload sent as query params.
Each LangGraph node emits events via EventEmitter → SSE → client.

Register in app/main.py:
    from app.api.v1 import text_stream
    app.include_router(text_stream.router, prefix="/api/v1/pipeline", tags=["pipeline"])
"""

import asyncio
import json
import logging
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import brand_profiles
from app.db.redis import get_cache, set_cache
from app.models.text import GenerateTextRequest, InputSourceType, ToneOverride, ScheduleMode
from app.shared.language import detect_language, first_present_or_none, user_language, workspace_language
from app.agents.text.event_emitter import EventEmitter
from app.pipelines.text.orchestrator import run_text_pipeline

router = APIRouter()
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Active sessions — maps session_id → (EventEmitter, workspace_id)
# Used by resume endpoint to unblock paused pipelines. In-process only:
# the EventEmitter wraps a live asyncio.Queue/Event tied to this process's
# event loop and a running pipeline task, so it can never be handed to
# another process regardless of what's in Redis.
#
# Every session is *also* mirrored into Redis (see _register_session /
# _mark_session_ended below) purely as a status record — not the emitter,
# just "this session_id belongs to this workspace, and is active/ended".
# That's enough to turn a blind 404 into an accurate answer when a status
# or resume request arrives after this process has restarted (deploy,
# crash, OOM) since the session began: today's single-web-instance
# deployment (see DEPLOY.md) means that's the only way this dict's
# in-process-only nature actually bites, since there's no second instance
# to route to. It does NOT make a pipeline resumable after a restart —
# the blocked asyncio task and its call stack are gone the moment the
# process dies, Redis or not; genuine crash-resumable pipelines would
# need LangGraph-level checkpointing (interrupt()/Command(resume=...))
# around real pause points, which is a materially larger change than a
# status registry.
# ─────────────────────────────────────────────────────────────────────────────

_active_sessions: dict[str, tuple[EventEmitter, str]] = {}

_SESSION_TTL_SECONDS = 600           # generous ceiling: generation run + pause wait
_SESSION_ENDED_TTL_SECONDS = 120     # short-lived "this session is over" marker


def _session_cache_key(session_id: str) -> str:
    return f"pipeline_session:{session_id}"


async def _register_session(session_id: str, workspace_id: str) -> None:
    await set_cache(
        _session_cache_key(session_id),
        {"workspace_id": workspace_id, "status": "active"},
        ttl=_SESSION_TTL_SECONDS,
    )


async def _mark_session_ended(session_id: str, workspace_id: str) -> None:
    """Called whether the pipeline finished, errored, or the client just
    disconnected — in every case the session is no longer resumable, which
    is the one fact a status/resume check actually needs."""
    await set_cache(
        _session_cache_key(session_id),
        {"workspace_id": workspace_id, "status": "ended"},
        ttl=_SESSION_ENDED_TTL_SECONDS,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SSE streaming endpoint — GET so EventSource works in browser
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/generate/stream")
@limiter.limit("10/minute")
async def generate_stream(
    request:       Request,
    # Required query params
    content:       str            = Query(...,       description="Source content"),
    source_type:   str            = Query(...,       description="text | url | prompt | repurpose"),
    platforms:     str            = Query(...,       description="Comma-separated: linkedin,threads"),
    brand_id:      str            = Query(...,       description="Brand profile ID"),
    # Optional query params
    tone:          str            = Query("brand",  description="brand | casual | formal | witty | empathetic | bold"),
    batch_mode:    bool           = Query(False,     description="Enable batch mode"),
    goal:          Optional[str]  = Query(None,      description="awareness | engagement | conversion | retention | education"),
    schedule_mode: Optional[str]  = Query(None,      description="now | scheduled | draft"),
    scheduled_at:  Optional[str]  = Query(None,      description="ISO datetime for scheduled posts"),
    # None = no per-request preference stated; falls through to the same
    # workspace > caller-account > "en" precedence chain as /api/v1/text/generate
    # (see app.shared.language and api/v1/text.py::_resolve_request_language).
    language:      Optional[str]  = Query(None,       description="Content language — omit to use the workspace/account default"),
    ctx:           WorkspaceContext = Depends(require("create_content")),
) -> StreamingResponse:
    """
    Stream text pipeline execution via Server-Sent Events.

    Uses GET so native browser EventSource works.
    Payload sent as query parameters.

    Event types emitted:
        session_started   — immediately on connect
        pipeline_stage    — node status (queued/active/complete/failed)
        activity_log      — human-readable agent narration
        output_started    — agent began working on a platform
        content_chunk     — partial content for typewriter effect
        output_complete   — full platform output ready for approval
        agent_paused      — tie-break needs human input
        pipeline_complete — all platforms done
        pipeline_error    — something failed
        ping              — keepalive every 45s
    """
    # Validate brand is in the caller's workspace and complete
    brand = await brand_profiles.find_one({
        "id":           brand_id,
        "workspace_id": ctx.workspace_id,
    })
    if not brand:
        raise HTTPException(status_code=404, detail="Brand profile not found.")
    if not brand.get("is_complete"):
        raise HTTPException(status_code=400, detail="Brand profile is not complete.")

    # Precedence chain: explicit query param > workspace default > caller's
    # own account default > detected from `content` > "en" — identical order
    # to the JSON /generate endpoint (see
    # api/v1/text.py::_resolve_request_language).
    explicit_language = first_present_or_none(
        language,
        await workspace_language(ctx.workspace_id),
        await user_language(ctx.user_id),
    )
    effective_language = explicit_language or detect_language(content) or "en"

    # Build GenerateTextRequest from query params
    try:
        body = GenerateTextRequest(
            content=content,
            source_type=InputSourceType(source_type),
            platforms=platforms.split(","),
            brand_id=brand_id,
            tone=ToneOverride(tone) if tone else ToneOverride.BRAND,
            batch_mode=batch_mode,
            goal=goal,
            schedule_mode=ScheduleMode(schedule_mode) if schedule_mode else ScheduleMode.NOW,
            scheduled_at=scheduled_at,
            language=effective_language,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid parameter: {e}")

    emitter    = EventEmitter()
    session_id = str(uuid4())

    # Register for resume endpoint (scoped to this workspace)
    _active_sessions[session_id] = (emitter, ctx.workspace_id)
    await _register_session(session_id, ctx.workspace_id)

    async def event_stream():
        # Emit session started immediately so frontend can show queued cards
        yield _sse("session_started", {
            "session_id":     session_id,
            "platforms":      body.platforms,
            "platform_count": len(body.platforms),
        })

        # Start pipeline in background — does not block SSE stream
        pipeline_task = asyncio.create_task(
            _run_pipeline_with_emitter(
                body=body,
                emitter=emitter,
                session_id=session_id,
                workspace_id=ctx.workspace_id,
                user_id=ctx.user_id,
            )
        )

        # Forward events from queue to SSE stream
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        emitter.queue.get(),
                        timeout=45.0,
                    )

                    # Done sentinel — pipeline finished or errored
                    if event is EventEmitter.DONE:
                        break

                    yield _sse(event["type"], event["data"])

                    # Also stop on completion/error event type
                    if event["type"] in ("pipeline_complete", "pipeline_error"):
                        break

                except asyncio.TimeoutError:
                    # Keepalive ping — prevents nginx/proxy from closing connection
                    yield "event: ping\ndata: {}\n\n"

        except asyncio.CancelledError:
            logger.info("SSE client disconnected — session %s", session_id)
            pipeline_task.cancel()

        except Exception as exc:
            logger.error("SSE stream error — session %s: %s", session_id, exc)
            yield _sse("pipeline_error", {
                "message":     str(exc),
                "recoverable": False,
            })

        finally:
            _active_sessions.pop(session_id, None)
            await _mark_session_ended(session_id, ctx.workspace_id)
            if not pipeline_task.done():
                pipeline_task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",        # Nginx: disable response buffering
            "Connection":        "keep-alive",
            "X-Session-Id":      session_id,  # Frontend reads this for resume calls
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Resume endpoint — unblocks a paused pipeline
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/session/{session_id}/resume")
@limiter.limit("20/minute")
async def resume_pipeline(
    request:    Request,
    session_id: str,
    body:       dict[str, Any],
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """
    Resume a pipeline that emitted agent_paused.
    body: { "choice": "angle_1" }
    """
    entry = _active_sessions.get(session_id)
    if not entry or entry[1] != ctx.workspace_id:
        cached = await get_cache(_session_cache_key(session_id))
        if cached and cached.get("workspace_id") == ctx.workspace_id:
            if cached.get("status") == "ended":
                raise HTTPException(status_code=409, detail="Session already finished.")
            # Registered in Redis (so it did exist and belongs to this
            # workspace) but missing from this process's local dict —
            # the only way that happens is this process restarted since
            # the session began. The pipeline task died with it; there is
            # nothing left to resume.
            raise HTTPException(
                status_code=410,
                detail="Session was lost when the server restarted. Please start a new generation.",
            )
        raise HTTPException(
            status_code=404,
            detail="Session not found or already complete.",
        )
    emitter = entry[0]

    choice = body.get("choice")
    if not choice:
        raise HTTPException(status_code=400, detail="choice is required.")

    await emitter.resume(choice=choice)
    logger.info("Session %s resumed with choice: %s", session_id, choice)

    return {"resumed": True, "session_id": session_id, "choice": choice}


# ─────────────────────────────────────────────────────────────────────────────
# Session status — for reconnect after page refresh
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/session/{session_id}/status")
@limiter.limit("30/minute")
async def get_session_status(
    request:    Request,
    session_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Check if a session is still active. Frontend calls on page load."""
    entry = _active_sessions.get(session_id)
    if entry and entry[1] == ctx.workspace_id:
        return {
            "session_id": session_id,
            "active":     True,
            "can_resume": True,
            "status":     "active",
        }

    cached = await get_cache(_session_cache_key(session_id))
    if cached and cached.get("workspace_id") == ctx.workspace_id:
        # Known to Redis but not running locally — either it legitimately
        # ended, or this process restarted since the session began.
        status = "ended" if cached.get("status") == "ended" else "lost"
        return {"session_id": session_id, "active": False, "can_resume": False, "status": status}

    return {"session_id": session_id, "active": False, "can_resume": False, "status": "not_found"}


# ─────────────────────────────────────────────────────────────────────────────
# Internal — runs pipeline with emitter injected
# ─────────────────────────────────────────────────────────────────────────────

async def _run_pipeline_with_emitter(
    body:         GenerateTextRequest,
    emitter:      EventEmitter,
    session_id:   str,
    workspace_id: str,
    user_id:      str,
) -> None:
    """
    Wraps run_text_pipeline with error handling.
    All exceptions caught and emitted as pipeline_error events.
    """
    try:
        await run_text_pipeline(
            source_type=body.source_type,
            content=body.content,
            platforms=body.platforms,
            brand_id=body.brand_id,
            workspace_id=workspace_id,
            user_id=user_id,
            extras=body.extras,
            goal=body.goal,
            tone=body.tone,
            intent=body.intent,
            language=body.language,
            schedule_mode=body.schedule_mode.value if body.schedule_mode else "now",
            scheduled_at=body.scheduled_at,
            emitter=emitter,
            session_id=session_id,
        )
    except Exception as exc:
        logger.error(
            "Pipeline failed — session %s: %s",
            session_id, exc, exc_info=True,
        )
        await emitter.emit_error(message=str(exc), recoverable=False)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sse(event_type: str, data: dict[str, Any]) -> str:
    """Format a single SSE message string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"