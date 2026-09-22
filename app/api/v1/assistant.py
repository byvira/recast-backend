"""Personal-assistant API — member-scoped ("Remy").

Every route resolves the caller's active workspace and then hard-scopes to
``ctx.user_id``. A persona and its signals are private to the member they belong
to; no other member — and no workspace admin — can read them here.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.agents.personal import service
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import member_lexicon, member_voice_settings
from app.models.lexicon import MemberLexicon, MemberLexiconWrite
from app.models.voice_settings import MemberVoiceSettings, MemberVoiceSettingsWrite

router = APIRouter()
logger = logging.getLogger(__name__)


class AssistRequest(BaseModel):
    draft_text: str = Field(..., min_length=1)
    pipeline_type: str = "text"      # which pipeline the draft belongs to
    target: str = ""                 # platform / route label, opaque
    brand_id: Optional[str] = None   # accepted for forward-compat; not required today


@router.get("/persona")
@limiter.limit("30/minute")
async def get_my_persona(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The caller's own voice persona — style fingerprint, topics, volume,
    drift history. 404 until Remy has seen some of their content."""
    return await service.get_persona(ctx.workspace_id, ctx.user_id)


@router.get("/signals")
@limiter.limit("60/minute")
async def get_my_signals(
    request: Request,
    status: Optional[str] = Query(None, pattern="^(open|acknowledged|auto_resolved)$"),
    limit: int = Query(50, ge=1, le=200),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The caller's own assistant signals (voice drift, volume, topic, quality)."""
    return await service.list_signals(ctx.workspace_id, ctx.user_id, status=status, limit=limit)


@router.post("/signals/{signal_id}/acknowledge")
@limiter.limit("60/minute")
async def acknowledge_my_signal(
    request: Request,
    signal_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    return await service.acknowledge_signal(ctx.workspace_id, ctx.user_id, signal_id)


@router.post("/assist")
@limiter.limit("20/minute")
async def assist_with_draft(
    request: Request,
    body: AssistRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict:
    """Given a work-in-progress draft, tell the member whether it matches their
    established voice and how to pull it back if not. User-initiated — makes one
    Groq call. Never on any pipeline's latency path."""
    return await service.assist(
        ctx.workspace_id,
        ctx.user_id,
        pipeline_type=body.pipeline_type,
        draft_text=body.draft_text,
        target=body.target,
    )


def _voice_settings_id(workspace_id: str, user_id: str) -> str:
    return f"{workspace_id}:{user_id}"


@router.get("/voice-settings")
@limiter.limit("60/minute")
async def get_voice_settings(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> MemberVoiceSettings:
    """Scaffolding — real settings, persisted, but nothing synthesizes audio
    from them yet (no TTS engine wired in). Returns defaults on first call,
    same as a piece of user preference state with no explicit save yet."""
    doc = await member_voice_settings.find_one(
        {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id}
    )
    if doc:
        return MemberVoiceSettings(**doc)
    return MemberVoiceSettings(
        id=_voice_settings_id(ctx.workspace_id, ctx.user_id),
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )


@router.put("/voice-settings")
@limiter.limit("30/minute")
async def update_voice_settings(
    request: Request,
    body: MemberVoiceSettingsWrite,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> MemberVoiceSettings:
    now = datetime.now(timezone.utc)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    await member_voice_settings.update_one(
        {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id},
        {
            "$set": {**updates, "updated_at": now},
            "$setOnInsert": {
                "id": _voice_settings_id(ctx.workspace_id, ctx.user_id),
                "workspace_id": ctx.workspace_id,
                "user_id": ctx.user_id,
                "created_at": now,
            },
        },
        upsert=True,
    )
    doc = await member_voice_settings.find_one(
        {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id}
    )
    return MemberVoiceSettings(**doc)


@router.get("/lexicon")
@limiter.limit("60/minute")
async def get_lexicon(
    request: Request,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> MemberLexicon:
    """Scaffolding — real pronunciation/jargon/writing-blueprint data,
    persisted, but nothing enforces it during generation yet."""
    doc = await member_lexicon.find_one(
        {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id}
    )
    if doc:
        return MemberLexicon(**doc)
    return MemberLexicon(
        id=_voice_settings_id(ctx.workspace_id, ctx.user_id),
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )


@router.put("/lexicon")
@limiter.limit("30/minute")
async def update_lexicon(
    request: Request,
    body: MemberLexiconWrite,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> MemberLexicon:
    """Full-document replace — see MemberLexiconWrite's docstring for why."""
    now = datetime.now(timezone.utc)
    # Pronunciation entries arrive without ids from a fresh "add" on the
    # frontend (client-side array push) — assign one server-side rather than
    # trusting the client to generate a collision-free id.
    pronunciations = [
        {**p.model_dump(), "id": p.id or str(uuid4())} for p in body.pronunciations
    ]
    await member_lexicon.update_one(
        {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id},
        {
            "$set": {
                "pronunciations": pronunciations,
                "whitelist": body.whitelist,
                "blacklist": body.blacklist,
                "writing_blueprint": body.writing_blueprint.model_dump(),
                "updated_at": now,
            },
            "$setOnInsert": {
                "id": _voice_settings_id(ctx.workspace_id, ctx.user_id),
                "workspace_id": ctx.workspace_id,
                "user_id": ctx.user_id,
                "created_at": now,
            },
        },
        upsert=True,
    )
    doc = await member_lexicon.find_one(
        {"workspace_id": ctx.workspace_id, "user_id": ctx.user_id}
    )
    return MemberLexicon(**doc)


@router.get("/nudge")
@limiter.limit("120/minute")
async def get_nudge(
    request: Request,
    pipeline_type: str = Query("text"),
    piece_id: Optional[str] = Query(None),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Lightweight, LLM-free alignment read for a piece — the same data attached
    inline to pipeline responses, exposed for the frontend to poll."""
    return await service.nudge(ctx.workspace_id, ctx.user_id, piece_id=piece_id)
