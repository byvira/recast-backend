"""AudioAsset API routes — Stage 4 scope only (see
pow/audio_image_pipeline/PROGRESS.md): real script -> TTS -> AudioAsset for
the simplest case (single-voice narration, no DSP yet) + basic export.

Real correction from the plan (GAPS.md G-7): does NOT route through the
connected LangGraph agent (app.pipelines.audio.orchestrator) — that agent
produces derived text from a transcript, not audio from a script. This
calls app.pipelines.media.tts_generation.synthesize_speech directly.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from app.agents.supervisor.service import assert_ai_budget_available, assert_generation_allowed
from app.api.v1.media import ALLOWED_MIME_TYPES, _max_bytes_for
from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import (
    audio_assets,
    audio_asset_versions,
    audio_share_links,
    content_pieces,
    guest_voice_profiles,
    media_assets,
    member_lexicon,
    member_voice_settings,
    podcast_feed_settings,
    users,
)
from app.models.agent_events import ContentEventPayload, ContentRef, EventType
from app.models.audio_asset import (
    AudioApprovalStatus,
    AudioAsset,
    AudioAssetVersion,
    AudioShareLink,
    AudioSourceType,
    GuestVoiceProfile,
    PodcastFeedEnableRequest,
    PodcastFeedSettings,
    PodcastFeedStatusResponse,
    PodcastFeedUpdateRequest,
    Signoff,
    SignoffRole,
)
from app.models.workspace import WorkspaceRole
from app.models.lexicon import MemberLexicon
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.models.voice_settings import MemberVoiceSettings
from app.pipelines.audio.transcriber import transcribe_audio_bytes
from app.pipelines.media.audio_enhance import concatenate_turns, enhance_audio, is_dsp_supported
from app.pipelines.media.tts_generation import is_language_supported, synthesize_speech
from app.pipelines.media.transform import build_export_url
from app.prompts.registry import load_prompt
from app.shared.events import emit_event_background
from app.shared.llm import call_llm, set_usage_workspace
from app.shared.pipeline_types import PipelineType
from app.shared.storage import ContentType as UploadContentType, upload_file

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_voice_settings(workspace_id: str, user_id: str) -> MemberVoiceSettings:
    """Real, already-persisted per-member settings (Remy's Narration Voice
    tab) — falls back to real defaults if the member never customized
    them, same as every other real-default-then-override pattern in this
    codebase (never a hard failure over an unset preference)."""
    doc = await member_voice_settings.find_one({"workspace_id": workspace_id, "user_id": user_id})
    if doc:
        return MemberVoiceSettings(**doc)
    return MemberVoiceSettings(id=f"{workspace_id}:{user_id}", workspace_id=workspace_id, user_id=user_id)


async def _record_initial_version(asset: AudioAsset) -> None:
    """Version 1 = the asset as first created. Nothing else ever starts a
    version history (the only writer, _bump_audio_version, is called only
    from restore, which itself needs an existing version to restore), so
    without this the versions list was permanently empty and restore
    permanently 404."""
    await audio_asset_versions.insert_one(AudioAssetVersion(
        version_id=str(uuid4()),
        audio_asset_id=asset.id,
        workspace_id=asset.workspace_id,
        user_id=asset.created_by,
        version_number=1,
        script_snapshot=asset.script,
        media_id=asset.media_id,
        action="created",
        created_at=asset.created_at,
    ).model_dump())


async def _get_lexicon(workspace_id: str, user_id: str) -> Optional[MemberLexicon]:
    """Real, already-persisted per-member lexicon (Remy's Vocabulary &
    Pronunciation tab). None (not a default instance) when the member has
    never saved one — synthesize_speech treats that the same as "no
    pronunciation locators", so this stays a pure optional lookup."""
    doc = await member_lexicon.find_one({"workspace_id": workspace_id, "user_id": user_id})
    return MemberLexicon(**doc) if doc else None


class GenerateAudioAssetRequest(BaseModel):
    title: str
    brand_id: str
    script: Optional[str] = None
    source_piece_id: Optional[str] = None


@router.post("/generate", response_model=AudioAsset, status_code=201)
@limiter.limit("10/minute")
async def generate_audio_asset(
    request: Request,
    body: GenerateAudioAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioAsset:
    """Real script -> TTS -> AudioAsset. `source_piece_id` (file 04 Part 1):
    when given and `script` is omitted, uses that piece's real content
    directly as the script — the plan's own "optionally passed through a
    short reformatting pass" is a real Phase-2 enhancement, deliberately
    skipped here to keep this slice minimal but real, not faked.
    """
    await assert_generation_allowed(ctx.workspace_id)

    script = (body.script or "").strip()
    source_content_hash: Optional[str] = None

    if body.source_piece_id:
        piece = await content_pieces.find_one(
            {"piece_id": body.source_piece_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}
        )
        if not piece:
            raise HTTPException(status_code=404, detail="Source piece not found.")
        content = piece.get("content") or ""
        source_content_hash = str(hash(content))
        if not script:
            script = content.strip()

    if not script:
        raise HTTPException(
            status_code=400, detail="script is required (or pass source_piece_id to use its content)."
        )

    voice_settings = await _get_voice_settings(ctx.workspace_id, ctx.user_id)
    lexicon = await _get_lexicon(ctx.workspace_id, ctx.user_id)
    audio_bytes = await synthesize_speech(
        text=script,
        voice_settings=voice_settings,
        lexicon=lexicon,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    if not audio_bytes:
        raise HTTPException(
            status_code=503,
            detail=(
                "Real narration couldn't be synthesized right now — no ElevenLabs "
                "key configured, no real voice set up yet, or the provider call "
                "itself failed. Nothing was created."
            ),
        )

    url = await upload_file(audio_bytes, UploadContentType.AUDIO, ctx.user_id)
    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=MediaKind.AUDIO,
        url=url,
        mime_type="audio/mpeg",
        source=MediaSource.SYNTHESIZED,
        created_by=ctx.user_id,
        created_at=now,
    )
    await media_assets.insert_one(media.model_dump())

    asset_id = str(uuid4())
    asset = AudioAsset(
        id=asset_id,
        workspace_id=ctx.workspace_id,
        brand_id=body.brand_id,
        created_by=ctx.user_id,
        created_at=now,
        updated_at=now,
        title=body.title,
        source_type=AudioSourceType.SCRIPT_TTS,
        script=script,
        voice_settings_snapshot=voice_settings.model_dump(),
        media_id=media.id,
        approval_status=AudioApprovalStatus.PENDING,
        source_piece_id=body.source_piece_id,
        source_content_hash=source_content_hash,
    )
    await audio_assets.insert_one(asset.model_dump())
    await _record_initial_version(asset)

    # file 04 Part 2 — first-class Phase 1 scope, not deferred, same as Image.
    emit_event_background(
        event_type=EventType.CONTENT_CREATED,
        pipeline_type=PipelineType.AUDIO,
        workspace_id=ctx.workspace_id,
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
        payload=ContentEventPayload(
            content_id=asset_id,
            content_ref=ContentRef(collection="audio_assets", id=asset_id),
            content_text=script,
            content_summary=script[:400],
            brand_id=body.brand_id,
        ),
    )

    return asset


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-VOICE DIALOGUE — closes the "multi-voice dialogue" gap named in
# PROGRESS.md's Deferred list. Real use: a scripted conversation (a host +
# guest, or several co-hosts) with each speaker in a distinct real voice,
# stitched into one continuous file — not a live recording, a written
# script turned into a real multi-voice audio piece.
# ─────────────────────────────────────────────────────────────────────────────

class DialogueTurn(BaseModel):
    speaker: str
    text: str
    # A real ElevenLabs voice_id or Deepgram Aura-2 model name for this
    # turn's speaker. Omitted for a turn spoken by the calling member in
    # their own already-configured voice (_get_voice_settings) — a guest
    # speaker always needs one, since there's no workspace-wide setting
    # for a non-member.
    voice_id: Optional[str] = None


class GenerateDialogueRequest(BaseModel):
    title: str
    brand_id: str
    turns: list[DialogueTurn]
    source_piece_id: Optional[str] = None


@router.post("/dialogue", response_model=AudioAsset, status_code=201)
@limiter.limit("5/minute")
async def generate_dialogue(
    request: Request,
    body: GenerateDialogueRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioAsset:
    """Synthesizes each turn (member's own voice when no voice_id is
    given, the turn's real voice_id otherwise via an ad-hoc
    MemberVoiceSettings — synthesize_speech needs no second code path for
    a non-member voice), then stitches them into one real continuous
    file via concatenate_turns. A named guest speaker's voice_id is saved
    as a real GuestVoiceProfile, scoped to this one AudioAsset, so the
    same guest can be reused across turns without repeating the id."""
    await assert_generation_allowed(ctx.workspace_id)

    if not body.turns:
        raise HTTPException(status_code=400, detail="At least one turn is required.")

    member_voice_settings_obj = await _get_voice_settings(ctx.workspace_id, ctx.user_id)
    member_lexicon_obj = await _get_lexicon(ctx.workspace_id, ctx.user_id)

    turn_audio: list[bytes] = []
    guest_voices_seen: dict[str, str] = {}
    for turn in body.turns:
        # A guest speaker has no lexicon of their own — the member's
        # pronunciation dictionary applies only when the member's own
        # voice is actually reading the line.
        turn_lexicon: Optional[MemberLexicon] = None
        if turn.voice_id:
            voice_settings = MemberVoiceSettings(
                id=f"guest:{turn.speaker}", workspace_id=ctx.workspace_id, user_id=ctx.user_id,
                tts_voice=turn.voice_id,
            )
            guest_voices_seen[turn.speaker] = turn.voice_id
        else:
            voice_settings = member_voice_settings_obj
            turn_lexicon = member_lexicon_obj

        audio_bytes = await synthesize_speech(
            text=turn.text, voice_settings=voice_settings, lexicon=turn_lexicon,
            workspace_id=ctx.workspace_id, user_id=ctx.user_id,
        )
        if not audio_bytes:
            raise HTTPException(
                status_code=503,
                detail=f"Couldn't synthesize the turn for '{turn.speaker}' — no real voice/provider available.",
            )
        turn_audio.append(audio_bytes)

    try:
        combined_bytes = concatenate_turns(turn_audio)
    except Exception as exc:  # noqa: BLE001
        logger.error("Dialogue stitching failed for workspace %s: %s", ctx.workspace_id, exc)
        raise HTTPException(status_code=502, detail="Couldn't combine the dialogue turns into one file.")

    url = await upload_file(combined_bytes, UploadContentType.AUDIO, ctx.user_id)
    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=MediaKind.AUDIO,
        url=url,
        mime_type="audio/wav",
        source=MediaSource.SYNTHESIZED,
        created_by=ctx.user_id,
        created_at=now,
    )
    await media_assets.insert_one(media.model_dump())

    script = "\n".join(f"{t.speaker}: {t.text}" for t in body.turns)
    asset_id = str(uuid4())
    asset = AudioAsset(
        id=asset_id,
        workspace_id=ctx.workspace_id,
        brand_id=body.brand_id,
        created_by=ctx.user_id,
        created_at=now,
        updated_at=now,
        title=body.title,
        source_type=AudioSourceType.DIALOGUE,
        script=script,
        media_id=media.id,
        approval_status=AudioApprovalStatus.PENDING,
        source_piece_id=body.source_piece_id,
    )
    await audio_assets.insert_one(asset.model_dump())
    await _record_initial_version(asset)

    if guest_voices_seen:
        await guest_voice_profiles.insert_many([
            GuestVoiceProfile(
                id=str(uuid4()), audio_asset_id=asset_id, workspace_id=ctx.workspace_id,
                name=name, voice_id=voice_id, created_at=now,
            ).model_dump()
            for name, voice_id in guest_voices_seen.items()
        ])

    emit_event_background(
        event_type=EventType.CONTENT_CREATED,
        pipeline_type=PipelineType.AUDIO,
        workspace_id=ctx.workspace_id,
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
        payload=ContentEventPayload(
            content_id=asset_id,
            content_ref=ContentRef(collection="audio_assets", id=asset_id),
            content_text=script,
            content_summary=script[:400],
            brand_id=body.brand_id,
        ),
    )

    return asset


# ─────────────────────────────────────────────────────────────────────────────
# LOCALIZATION — closes the transcribe+translate+re-narrate slice of the
# real gap named in PROGRESS.md's Deferred list ("Audio, Phase 2.5 —
# Localization"). Real, honest scope: translates the script/transcript and
# re-synthesizes it for real — it does NOT clone the original speaker's
# voice (ElevenLabs voice cloning is a separate paid feature, not enabled
# on this account yet, see PROGRESS.md's Blockers section) — the new-
# language audio uses the caller's own already-configured voice, which
# will sound like a different voice, not the same speaker in a new
# language. Real product value regardless (translated narration a member
# can review/re-record over, not a placeholder), just not full dubbing.
# ─────────────────────────────────────────────────────────────────────────────

class LocalizeAudioAssetRequest(BaseModel):
    # Full language name (e.g. "Tamil", "French"), not an ISO code — both
    # the translation prompt and is_language_supported's real coverage
    # lists key on the full name.
    target_language: str


_TRANSLATION_QUALITY_THRESHOLD = 0.75
_TRANSLATION_MAX_ATTEMPTS = 3  # same shape as app.agents.base's real retry gate (max_retries=2 -> 3 total tries)


def _parse_translation_score(raw: str) -> tuple[float, str]:
    """Parses "SCORE: 0.9 REASON: ..." — defensive against a malformed
    reply (missing/non-numeric score) rather than raising, since a QA
    gate that crashes on its own scorer's bad output would block real
    work over a formatting slip."""
    try:
        score_part = raw.split("REASON:")[0]
        score = float(score_part.split("SCORE:")[1].strip())
        reason = raw.split("REASON:", 1)[1].strip() if "REASON:" in raw else ""
        return max(0.0, min(1.0, score)), reason
    except Exception:  # noqa: BLE001
        return 0.0, "Couldn't parse the QA gate's score — treated as failing."


async def _translate_with_quality_gate(source_text: str, target_language: str) -> tuple[str, float]:
    """Real 3x QA gate, same shape as app.agents.base.should_retry /
    app/agents/*/graph.py's evaluate->retry loop (avg_quality < 0.75,
    up to max_retries) — translate, score the translation's real
    accuracy/naturalness via a second LLM call, retry up to
    _TRANSLATION_MAX_ATTEMPTS total attempts, keep whichever attempt
    scored highest rather than always keeping the last one."""
    best_text, best_score = "", -1.0
    for attempt in range(1, _TRANSLATION_MAX_ATTEMPTS + 1):
        translate_prompt = load_prompt(
            "audio/localize/translate_script", target_language=target_language, script=source_text
        )
        candidate = (await call_llm(translate_prompt)).strip()
        if not candidate:
            continue

        score_prompt = load_prompt(
            "audio/localize/score_translation",
            source_text=source_text, target_language=target_language, translated_text=candidate,
        )
        score, reason = _parse_translation_score((await call_llm(score_prompt)).strip())
        logger.info(
            "Translation QA gate attempt %d/%d for target_language=%s: score=%.2f (%s)",
            attempt, _TRANSLATION_MAX_ATTEMPTS, target_language, score, reason,
        )

        if score > best_score:
            best_text, best_score = candidate, score
        if score >= _TRANSLATION_QUALITY_THRESHOLD:
            break

    return best_text, best_score


@router.post("/{audio_asset_id}/localize", response_model=AudioAsset, status_code=201)
@limiter.limit("5/minute")
async def localize_audio_asset(
    request: Request,
    audio_asset_id: str,
    body: LocalizeAudioAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioAsset:
    """Real translate + re-synthesize, gated by a real 3x translation QA
    check (see _translate_with_quality_gate). Source text comes from the
    real script when one exists (SCRIPT_TTS/DIALOGUE); for an UPLOADED
    asset with no script, falls back to the real transcript (word-level
    -> joined text) — a real 400 if neither exists, not a silent empty
    translation.

    Real pre-check (2026-09-26): fails clearly with a specific reason if
    the target language isn't actually synthesizable by whichever TTS
    provider is really reachable right now — confirmed live against each
    provider's own docs that Deepgram's 7-language Aura model does NOT
    cover Tamil (or most languages ElevenLabs' 29-language model does),
    so requesting Tamil while ElevenLabs is still blocked on the free
    plan needs a real, specific error, not a confusing failure three
    steps later inside the TTS call.
    """
    await assert_generation_allowed(ctx.workspace_id)
    await assert_ai_budget_available(ctx.workspace_id)
    # Translation is a real LLM spend: attribute it to this workspace so it
    # counts toward (and is limited by) the AI budget like Text generation.
    set_usage_workspace(ctx.workspace_id)

    is_supported, reason = is_language_supported(body.target_language)
    if not is_supported:
        raise HTTPException(status_code=400, detail=reason)

    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    source = AudioAsset(**doc)

    source_text = (source.script or "").strip()
    if not source_text and source.transcript:
        source_text = " ".join(w["word"] if isinstance(w, dict) else w.word for w in source.transcript)
    if not source_text:
        raise HTTPException(
            status_code=400,
            detail="This audio asset has no script or transcript to translate.",
        )

    translated_text, quality_score = await _translate_with_quality_gate(source_text, body.target_language)
    if not translated_text:
        raise HTTPException(status_code=502, detail="Translation failed. Try again.")
    if quality_score < _TRANSLATION_QUALITY_THRESHOLD:
        logger.warning(
            "Translation for audio_asset %s never reached the quality threshold after %d attempts "
            "(best score %.2f) — proceeding with the best attempt, flagged for review.",
            audio_asset_id, _TRANSLATION_MAX_ATTEMPTS, quality_score,
        )

    voice_settings = await _get_voice_settings(ctx.workspace_id, ctx.user_id)
    lexicon = await _get_lexicon(ctx.workspace_id, ctx.user_id)
    audio_bytes = await synthesize_speech(
        text=translated_text, voice_settings=voice_settings, lexicon=lexicon,
        workspace_id=ctx.workspace_id, user_id=ctx.user_id,
    )
    if not audio_bytes:
        raise HTTPException(
            status_code=503,
            detail="Translation succeeded but narration couldn't be synthesized right now.",
        )

    url = await upload_file(audio_bytes, UploadContentType.AUDIO, ctx.user_id)
    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=MediaKind.AUDIO,
        url=url,
        mime_type="audio/mpeg",
        source=MediaSource.SYNTHESIZED,
        created_by=ctx.user_id,
        created_at=now,
    )
    await media_assets.insert_one(media.model_dump())

    localized_id = str(uuid4())
    localized = AudioAsset(
        id=localized_id,
        workspace_id=ctx.workspace_id,
        brand_id=source.brand_id,
        created_by=ctx.user_id,
        created_at=now,
        updated_at=now,
        title=f"{source.title} ({body.target_language})",
        source_type=source.source_type,
        script=translated_text,
        media_id=media.id,
        approval_status=AudioApprovalStatus.PENDING,
        language=body.target_language,
        source_audio_asset_id=audio_asset_id,
    )
    await audio_assets.insert_one(localized.model_dump())
    await _record_initial_version(localized)

    emit_event_background(
        event_type=EventType.CONTENT_CREATED,
        pipeline_type=PipelineType.AUDIO,
        workspace_id=ctx.workspace_id,
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
        payload=ContentEventPayload(
            content_id=localized_id,
            content_ref=ContentRef(collection="audio_assets", id=localized_id),
            content_text=translated_text,
            content_summary=translated_text[:400],
            brand_id=source.brand_id,
        ),
    )

    return localized


# Real starting point without any TTS provider at all — an already-
# recorded file (Riverside/Zoom/phone/anything), transcribed+enhanced
# later (Phase 2), not synthesized now. Same real mime-type/size
# validation media.py's own upload_media already enforces, reused
# directly rather than duplicated.
_AUDIO_MIME_TYPES = ALLOWED_MIME_TYPES[MediaKind.AUDIO]


@router.post("/upload", response_model=AudioAsset, status_code=201)
@limiter.limit("20/minute")
async def upload_audio_asset(
    request: Request,
    title: str = Form(...),
    brand_id: str = Form(...),
    file: UploadFile = File(...),
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioAsset:
    """Real upload -> AudioAsset(source_type=UPLOADED). No TTS call at
    all — sidesteps the ElevenLabs/Azure provider question entirely for
    a user who already has a real recording.

    Real transcription-in + DSP cleanup (2026-09-26, bugs/gaps sweep):
    the upload is transcribed via Groq Whisper (word-level, matching
    AudioAsset.transcript's real shape) and denoise+loudness-normalized
    via app.pipelines.media.audio_enhance for WAV/MP3/OGG (soundfile's
    real decode support — see that module's own docstring for why M4A/
    WEBM aren't included). Both best-effort: a transcription or DSP
    failure never blocks the upload itself — the asset is still created,
    just with an empty transcript/original media, an honest partial
    result rather than a hard failure over a real recording someone
    already has in hand.
    """
    if (file.content_type or "") not in _AUDIO_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio type '{file.content_type}'. Allowed: {', '.join(sorted(_AUDIO_MIME_TYPES))}.",
        )

    contents = await file.read()
    max_bytes = await _max_bytes_for(MediaKind.AUDIO, ctx.workspace_id)
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=400, detail=f"Audio must be {max_bytes // (1024 * 1024)}MB or smaller."
        )

    dsp_settings: dict = {}
    media_bytes = contents
    media_mime = file.content_type
    if is_dsp_supported(file.content_type or ""):
        try:
            media_bytes, dsp_settings = enhance_audio(contents, file.content_type or "")
            media_mime = "audio/wav"
        except Exception as exc:  # noqa: BLE001
            logger.warning("DSP enhancement failed, using original upload: %s", exc)
            media_bytes, media_mime, dsp_settings = contents, file.content_type, {}

    try:
        url = await upload_file(media_bytes, UploadContentType.AUDIO, ctx.user_id)
    except Exception as exc:  # noqa: BLE001 — same tolerance as media.py's own upload_media
        logger.error("Audio upload failed for workspace %s: %s", ctx.workspace_id, exc)
        raise HTTPException(status_code=502, detail="Audio upload failed. Try again.")

    transcript = await transcribe_audio_bytes(contents, filename=file.filename or "audio.mp3")

    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=MediaKind.AUDIO,
        url=url,
        mime_type=media_mime,
        source=MediaSource.ENHANCED if dsp_settings else MediaSource.UPLOADED,
        created_by=ctx.user_id,
        created_at=now,
    )
    await media_assets.insert_one(media.model_dump())

    asset_id = str(uuid4())
    asset = AudioAsset(
        id=asset_id,
        workspace_id=ctx.workspace_id,
        brand_id=brand_id,
        created_by=ctx.user_id,
        created_at=now,
        updated_at=now,
        title=title,
        source_type=AudioSourceType.UPLOADED,
        media_id=media.id,
        transcript=transcript,
        dsp_settings=dsp_settings,
        approval_status=AudioApprovalStatus.PENDING,
    )
    await audio_assets.insert_one(asset.model_dump())
    await _record_initial_version(asset)

    # What the recording actually says is the only "text" an uploaded file
    # has. It used to be sent as "" (title only), so Odette's digest and
    # Remy's history could never read anything a member uploaded.
    spoken = " ".join(w.word.strip() for w in transcript if w.word.strip())
    emit_event_background(
        event_type=EventType.CONTENT_CREATED,
        pipeline_type=PipelineType.AUDIO,
        workspace_id=ctx.workspace_id,
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
        payload=ContentEventPayload(
            content_id=asset_id,
            content_ref=ContentRef(collection="audio_assets", id=asset_id),
            content_text=spoken,
            content_summary=spoken[:400] or title,
            brand_id=brand_id,
        ),
    )

    return asset


class ExportAudioAssetRequest(BaseModel):
    export_format: str = "mp3"  # "mp3" | "wav"


_AUDIO_EXPORT_FORMATS = {"mp3": "mp3", "wav": "wav"}


@router.post("/{audio_asset_id}/export", response_model=MediaAsset, status_code=201)
@limiter.limit("30/minute")
async def export_audio_asset(
    request: Request,
    audio_asset_id: str,
    body: ExportAudioAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> MediaAsset:
    """Format-conversion export via Cloudinary URL params, same convention
    as image_assets.export_image_asset."""
    if body.export_format not in _AUDIO_EXPORT_FORMATS:
        raise HTTPException(
            status_code=400, detail=f"Unsupported export format. Choose one of: mp3, wav."
        )

    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    asset = AudioAsset(**doc)
    if not asset.media_id:
        raise HTTPException(status_code=400, detail="This audio asset has no synthesized file to export.")

    source_doc = await media_assets.find_one({"id": asset.media_id, "workspace_id": ctx.workspace_id})
    if not source_doc:
        raise HTTPException(status_code=404, detail="The synthesized file is missing.")
    source = MediaAsset(**source_doc)

    try:
        new_url = build_export_url(source.url, export_format=body.export_format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    export_asset = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=MediaKind.AUDIO,
        url=new_url,
        mime_type=f"audio/{body.export_format}",
        source=MediaSource.EDITED,
        created_by=ctx.user_id,
        created_at=datetime.now(timezone.utc),
    )
    await media_assets.insert_one(export_asset.model_dump())
    return export_asset


# ─────────────────────────────────────────────────────────────────────────────
# GOVERNANCE — approve/reject/version-history/restore/share-link. Mirrors
# app/api/v1/image_assets.py's real pattern exactly, adapted for a single
# media_id + script_snapshot instead of a slides array — same verbs, same
# status semantics, not a new governance shape invented for Audio.
# ─────────────────────────────────────────────────────────────────────────────

async def _bump_audio_version(
    asset_id: str, workspace_id: str, new_media_id: str, script_snapshot: Optional[str], action: str, actor_user_id: str
) -> Optional[dict]:
    doc = await audio_assets.find_one({"id": asset_id, "workspace_id": workspace_id})
    if not doc:
        return None
    new_version_number = doc.get("version_count", 1) + 1
    now = datetime.now(timezone.utc)
    await audio_assets.update_one(
        {"id": asset_id, "workspace_id": workspace_id},
        {"$set": {"media_id": new_media_id, "version_count": new_version_number, "updated_at": now}},
    )
    version = AudioAssetVersion(
        version_id=str(uuid4()),
        audio_asset_id=asset_id,
        workspace_id=workspace_id,
        user_id=actor_user_id,
        version_number=new_version_number,
        script_snapshot=script_snapshot,
        media_id=new_media_id,
        action=action,
        created_at=now,
    )
    await audio_asset_versions.insert_one(version.model_dump())
    return await audio_assets.find_one({"id": asset_id, "workspace_id": workspace_id})


@router.patch("/{audio_asset_id}/approve", response_model=AudioAsset)
@limiter.limit("30/minute")
async def approve_audio_asset(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> AudioAsset:
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    asset = AudioAsset(**doc)
    await audio_assets.update_one(
        {"id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {
            "approval_status": AudioApprovalStatus.APPROVED.value,
            "approved_master_media_id": asset.media_id,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    updated = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    return AudioAsset(**updated)


@router.patch("/{audio_asset_id}/reject", response_model=AudioAsset)
@limiter.limit("30/minute")
async def reject_audio_asset(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> AudioAsset:
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    await audio_assets.update_one(
        {"id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {
            "approval_status": AudioApprovalStatus.REJECTED.value,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    updated = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    return AudioAsset(**updated)


@router.get("/")
@limiter.limit("60/minute")
async def list_audio_assets(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The workspace's audio, newest first, each with its playable file, for
    the Library's Audio tab. Read only."""
    flt = {"workspace_id": ctx.workspace_id}
    docs = await audio_assets.find(flt, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)
    total = await audio_assets.count_documents(flt)

    media_ids = [m for m in ((d.get("approved_master_media_id") or d.get("media_id")) for d in docs) if m]
    media_by_id: dict = {}
    if media_ids:
        found = await media_assets.find(
            {"id": {"$in": media_ids}, "workspace_id": ctx.workspace_id}, {"_id": 0}
        ).to_list(length=len(media_ids))
        media_by_id = {m["id"]: m for m in found}

    items = []
    for d in docs:
        media_id = d.get("approved_master_media_id") or d.get("media_id")
        words = [w.get("word", "") for w in (d.get("transcript") or [])[:40]]
        items.append({
            "id": d["id"],
            "title": d.get("title", ""),
            "source_type": d.get("source_type"),
            "created_at": d.get("created_at"),
            "approval_status": d.get("approval_status"),
            "language": d.get("language"),
            "excerpt": " ".join(words).strip() or (d.get("script") or "")[:200],
            "media": media_by_id.get(media_id) if media_id else None,
        })
    return {"items": items, "total": total}


@router.get("/{audio_asset_id}/versions")
@limiter.limit("60/minute")
async def list_audio_versions(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    versions = await audio_asset_versions.find(
        {"audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id}
    ).sort("version_number", 1).to_list(length=100)
    for v in versions:
        v.pop("_id", None)
    return {"audio_asset_id": audio_asset_id, "versions": versions, "total": len(versions)}


@router.post("/{audio_asset_id}/restore/{version_number}", response_model=AudioAsset)
@limiter.limit("20/minute")
async def restore_audio_version(
    request: Request,
    audio_asset_id: str,
    version_number: int,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> AudioAsset:
    """Restores media_id to what it actually was at that version. Real,
    honest limitation: this restores which rendered file is active, not
    a resynthesis from script_snapshot — matching the same "restore the
    real artifact, don't silently regenerate a new one" behavior Image's
    slide restore uses (its slides_snapshot already stores media_id
    references, never triggers a fresh render)."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    version_doc = await audio_asset_versions.find_one({
        "audio_asset_id": audio_asset_id,
        "workspace_id": ctx.workspace_id,
        "version_number": version_number,
    })
    if not version_doc:
        raise HTTPException(status_code=404, detail=f"Version {version_number} not found.")
    if not version_doc.get("media_id"):
        raise HTTPException(
            status_code=400, detail=f"Version {version_number} has no media file recorded to restore."
        )

    updated_doc = await _bump_audio_version(
        audio_asset_id, ctx.workspace_id, version_doc["media_id"], version_doc.get("script_snapshot"),
        f"restored_from_v{version_number}", ctx.user_id,
    )
    return AudioAsset(**updated_doc)


class AudioShareLinkResponse(BaseModel):
    token: str
    url: str
    expires_at: datetime


@router.post("/{audio_asset_id}/share-link", response_model=AudioShareLinkResponse, status_code=201)
@limiter.limit("10/minute")
async def create_audio_share_link(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioShareLinkResponse:
    """Real token + expiry, mirroring image_assets.create_image_share_link
    exactly. Same known, honest limitation: no public `/share/[token]`
    frontend page consumes this token yet."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")

    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=30)
    link = AudioShareLink(
        token=token,
        audio_asset_id=audio_asset_id,
        workspace_id=ctx.workspace_id,
        created_by=ctx.user_id,
        created_at=now,
        expires_at=expires_at,
    )
    await audio_share_links.insert_one(link.model_dump())
    return AudioShareLinkResponse(token=token, url=f"{settings.FRONTEND_URL}/share/{token}", expires_at=expires_at)


# ─────────────────────────────────────────────────────────────────────────────
# Real role signoffs — the Governance panel's own "Role Review Signoffs
# (4 Required)" already named these roles; each entry here is one real
# member's real signoff against one, not a locally-toggled checkbox.
# ─────────────────────────────────────────────────────────────────────────────

class SignoffRequest(BaseModel):
    role: SignoffRole


@router.post("/{audio_asset_id}/signoff", response_model=AudioAsset)
@limiter.limit("30/minute")
async def sign_off_audio_asset(
    request: Request,
    audio_asset_id: str,
    body: SignoffRequest,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> AudioAsset:
    """Idempotent per role — signing a role you (or someone else) already
    signed just replaces that entry with your own, real timestamp."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")

    user = await users.find_one({"id": ctx.user_id}, {"name": 1, "email": 1})
    user_name = ((user or {}).get("name") or (user or {}).get("email") or "Member")

    signoff = Signoff(role=body.role, user_id=ctx.user_id, user_name=user_name, signed_at=datetime.now(timezone.utc))
    remaining = [s for s in doc.get("signoffs", []) if s.get("role") != body.role.value]
    remaining.append(signoff.model_dump())
    await audio_assets.update_one(
        {"id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {"signoffs": remaining, "updated_at": datetime.now(timezone.utc)}},
    )
    updated = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    return AudioAsset(**updated)


@router.delete("/{audio_asset_id}/signoff/{role}", response_model=AudioAsset)
@limiter.limit("30/minute")
async def undo_signoff(
    request: Request,
    audio_asset_id: str,
    role: SignoffRole,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> AudioAsset:
    """The person who signed can undo their own signoff; an owner/admin
    can undo anyone's — the same real-membership check as elsewhere,
    not a role any editor can override."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")

    existing = next((s for s in doc.get("signoffs", []) if s.get("role") == role.value), None)
    if not existing:
        raise HTTPException(status_code=404, detail=f"'{role.value}' hasn't been signed off yet.")
    if existing.get("user_id") != ctx.user_id and ctx.role not in (WorkspaceRole.OWNER.value, WorkspaceRole.ADMIN.value):
        raise HTTPException(status_code=403, detail="Only the person who signed this off, or a workspace owner/admin, can undo it.")

    remaining = [s for s in doc.get("signoffs", []) if s.get("role") != role.value]
    await audio_assets.update_one(
        {"id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {"signoffs": remaining, "updated_at": datetime.now(timezone.utc)}},
    )
    updated = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    return AudioAsset(**updated)


# ─────────────────────────────────────────────────────────────────────────────
# Real podcast RSS feed — one per brand. Spotify for Podcasters and Apple
# Podcasts Connect don't offer a push API for a third-party host to
# dispatch episodes into; the real mechanism is a feed URL you submit once,
# which they then poll. This serves that real feed, not a fake "Connected"
# integration.
# ─────────────────────────────────────────────────────────────────────────────

def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&apos;")
    )


async def _feed_episode_count(brand_id: str) -> int:
    return await audio_assets.count_documents({"brand_id": brand_id, "approval_status": AudioApprovalStatus.APPROVED.value})


@router.post("/feed/enable", response_model=PodcastFeedStatusResponse, status_code=201)
@limiter.limit("10/minute")
async def enable_podcast_feed(
    request: Request,
    body: PodcastFeedEnableRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> PodcastFeedStatusResponse:
    existing = await podcast_feed_settings.find_one({"brand_id": body.brand_id, "workspace_id": ctx.workspace_id})
    now = datetime.now(timezone.utc)
    # The token stays stable across re-enables — a feed already submitted
    # to Spotify/Apple must keep resolving to the same URL.
    token = existing["token"] if existing else secrets.token_urlsafe(24)

    doc = PodcastFeedSettings(
        id=body.brand_id, workspace_id=ctx.workspace_id, brand_id=body.brand_id,
        token=token, title=body.title, description=body.description, is_enabled=True,
        created_at=(existing["created_at"] if existing else now), updated_at=now,
    )
    await podcast_feed_settings.update_one(
        {"brand_id": body.brand_id, "workspace_id": ctx.workspace_id},
        {"$set": doc.model_dump()},
        upsert=True,
    )
    episode_count = await _feed_episode_count(body.brand_id)
    return PodcastFeedStatusResponse(
        is_enabled=True, token=token, feed_url=f"{settings.FRONTEND_URL.rstrip('/')}/api/v1/audio-assets/feed/{token}.xml",
        title=body.title, description=body.description, episode_count=episode_count,
    )


@router.get("/feed/status", response_model=PodcastFeedStatusResponse)
@limiter.limit("30/minute")
async def get_podcast_feed_status(
    request: Request,
    brand_id: str = Query(...),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> PodcastFeedStatusResponse:
    doc = await podcast_feed_settings.find_one({"brand_id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        return PodcastFeedStatusResponse(is_enabled=False)
    episode_count = await _feed_episode_count(brand_id)
    return PodcastFeedStatusResponse(
        is_enabled=doc["is_enabled"], token=doc["token"],
        feed_url=f"{settings.FRONTEND_URL.rstrip('/')}/api/v1/audio-assets/feed/{doc['token']}.xml" if doc["is_enabled"] else None,
        title=doc.get("title", ""), description=doc.get("description", ""), episode_count=episode_count,
    )


@router.patch("/feed/{brand_id}", response_model=PodcastFeedStatusResponse)
@limiter.limit("20/minute")
async def update_podcast_feed(
    request: Request,
    brand_id: str,
    body: PodcastFeedUpdateRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> PodcastFeedStatusResponse:
    doc = await podcast_feed_settings.find_one({"brand_id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="No podcast feed set up for this brand yet.")

    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if body.title is not None:
        updates["title"] = body.title
    if body.description is not None:
        updates["description"] = body.description
    if body.is_enabled is not None:
        updates["is_enabled"] = body.is_enabled
    await podcast_feed_settings.update_one({"brand_id": brand_id, "workspace_id": ctx.workspace_id}, {"$set": updates})

    updated = await podcast_feed_settings.find_one({"brand_id": brand_id, "workspace_id": ctx.workspace_id})
    episode_count = await _feed_episode_count(brand_id)
    return PodcastFeedStatusResponse(
        is_enabled=updated["is_enabled"], token=updated["token"],
        feed_url=f"{settings.FRONTEND_URL.rstrip('/')}/api/v1/audio-assets/feed/{updated['token']}.xml" if updated["is_enabled"] else None,
        title=updated.get("title", ""), description=updated.get("description", ""), episode_count=episode_count,
    )


@router.get("/feed/{token}.xml")
@limiter.limit("120/minute")
async def get_podcast_feed_xml(request: Request, token: str) -> Response:
    """Public, unauthenticated — this is the real feed URL a member submits
    to Spotify for Podcasters / Apple Podcasts Connect once. Only real,
    approved episodes for that brand appear; nothing here is guessable
    (a 24-byte urlsafe token) or lists anything from another brand."""
    settings_doc = await podcast_feed_settings.find_one({"token": token})
    if not settings_doc or not settings_doc.get("is_enabled"):
        raise HTTPException(status_code=404, detail="This feed doesn't exist or isn't enabled.")

    episodes = await audio_assets.find(
        {"brand_id": settings_doc["brand_id"], "approval_status": AudioApprovalStatus.APPROVED.value},
    ).sort("created_at", -1).limit(200).to_list(length=200)

    media_ids = [e.get("approved_master_media_id") or e.get("media_id") for e in episodes]
    media_ids = [m for m in media_ids if m]
    media_by_id: dict = {}
    if media_ids:
        found = await media_assets.find({"id": {"$in": media_ids}}).to_list(length=len(media_ids))
        media_by_id = {m["id"]: m for m in found}

    items_xml = []
    for ep in episodes:
        media_id = ep.get("approved_master_media_id") or ep.get("media_id")
        media = media_by_id.get(media_id)
        if not media:
            continue  # no real playable file — never list an episode with nothing to play
        pub_date = ep["created_at"]
        if isinstance(pub_date, str):
            pub_date = datetime.fromisoformat(pub_date)
        items_xml.append(f"""    <item>
      <title>{_escape_xml(ep.get('title', 'Untitled episode'))}</title>
      <guid isPermaLink="false">{ep['id']}</guid>
      <pubDate>{pub_date.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>
      <enclosure url="{_escape_xml(media['url'])}" type="audio/mpeg" length="0" />
      <itunes:duration>{int(media.get('duration_s') or 0)}</itunes:duration>
    </item>""")

    title = _escape_xml(settings_doc.get("title") or "Podcast")
    description = _escape_xml(settings_doc.get("description") or "")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{title}</title>
    <description>{description}</description>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items_xml)}
  </channel>
</rss>"""
    return Response(content=xml, media_type="application/rss+xml")
