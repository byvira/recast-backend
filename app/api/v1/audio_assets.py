"""AudioAsset API routes — Stage 4 scope only (see
pow/audio_image_pipeline/PROGRESS.md): real script -> TTS -> AudioAsset for
the simplest case (single-voice narration, no DSP yet) + basic export.

Real correction from the plan (GAPS.md G-7): does NOT route through the
connected LangGraph agent (app.pipelines.audio.orchestrator) — that agent
produces derived text from a transcript, not audio from a script. This
calls app.pipelines.media.tts_generation.synthesize_speech directly.
"""

import asyncio
import io
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import httpx
import soundfile as sf
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app.agents.supervisor.service import assert_ai_budget_available, assert_generation_allowed
from app.api.v1.media import ALLOWED_MIME_TYPES, _max_bytes_for
from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import (
    audio_assets,
    audio_asset_versions,
    audio_comments,
    audio_kits,
    audio_share_links,
    brand_profiles,
    share_view_events,
    content_pieces,
    guest_voice_profiles,
    media_assets,
    member_lexicon,
    member_voice_settings,
    podcast_feed_settings,
    soundbites,
    users,
)
from app.models.agent_events import ContentEventPayload, ContentRef, EventType
from app.models.audio_asset import (
    Soundbite,
    SoundbiteOut,
    AudioApprovalStatus,
    AudioAsset,
    AudioAssetVersion,
    AudioComment,
    AudioCommentCreate,
    AudioCommentUpdate,
    AudioKit,
    AudioKitOut,
    AudioKitUpdate,
    AudioShareLink,
    Assembly,
    KitClip,
    KitClipOut,
    MusicBed,
    MusicBedCreate,
    MusicBedOut,
    AudioSourceType,
    GuestVoiceProfile,
    TranscriptWord,
    PODCAST_CATEGORIES,
    PodcastFeedChecklistItem,
    PodcastFeedEnableRequest,
    PodcastFeedSettings,
    PodcastFeedStatusResponse,
    PodcastFeedUpdateRequest,
    Signoff,
    SignoffRole,
    MakeVideoRequest,
    SuggestClipsResponse,
    SuggestedClip,
    VideoClip,
)
from app.models.workspace import WorkspaceRole
from app.models.lexicon import MemberLexicon
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.models.voice_settings import MemberVoiceSettings
from app.pipelines.audio.link_fetch import FeedListing, LinkFetchError, fetch_audio, list_feed_episodes
from app.pipelines.audio.transcriber import transcribe_audio_bytes
from app.pipelines.media.audio_assemble import AssembleError, AssemblePlan, assemble_episode
from app.pipelines.media.audio_cleanup import CleanupError, CleanupSettings, apply_cleanup
from app.pipelines.media.audio_enhance import concatenate_turns, enhance_audio, is_dsp_supported
from app.pipelines.media import chapters as chapter_rules
from app.pipelines.text.generator import resolve_language_directive_name
from app.shared.language import workspace_language
from app.pipelines.media import duration as spoken_length
from app.pipelines.media import fit_script as script_fitting
from app.pipelines.media.echo_reduction import BASIC_CLEANUP_NOTE, EchoReductionError, basic_cleanup, reduce_echo
from app.pipelines.media.music_library import list_library_tracks
from app.pipelines.media.soundbite_extraction import SoundbiteExtractionError, evaluate_quality, trim_span
from app.pipelines.media.tts_generation import is_language_supported, synthesize_speech, synthesize_speech_timed
from app.shared.localized_strings import clean_translation, looks_leaked
from app.pipelines.media.video_render import TranscriptWordLike, VideoRenderError, render_video
from app.pipelines.media import video_presets
from app.pipelines.media.transform import build_export_url
from app.prompts.registry import load_prompt
from app.shared.activity.runs import end_run, get_run, start_run, update_run
from app.shared.events import emit_event_background
from app.shared.llm import call_llm, call_llm_structured, set_usage_workspace
from app.shared.pipeline_types import PipelineType
from app.shared.storage import ContentType as UploadContentType, upload_file

logger = logging.getLogger(__name__)
router = APIRouter()



# ─────────────────────────────────────────────────────────────────────────────
# Live steps. Long operations report which step they are on to the same
# live-run registry the Activity popover reads (app.shared.activity.runs), and
# the Audio page's Processing Status polls GET /runs/{run_id} for it. The
# client names the run in an X-Run-Id header so it knows what to ask about.
# ─────────────────────────────────────────────────────────────────────────────

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class AudioRun:
    def __init__(self, workspace_id: str, run_id: str) -> None:
        self.workspace_id = workspace_id
        self.run_id = run_id
        self._done = 0

    async def step(self, label: str) -> None:
        """Marks the start of the next step; everything before it is done."""
        await update_run(self.workspace_id, self.run_id, stage=label, steps_done=self._done)
        self._done += 1


def audio_run(title: str, total: int):
    async def _dep(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)):
        header = (request.headers.get("x-run-id") or "").strip()
        run = AudioRun(ctx.workspace_id, header if _RUN_ID.match(header) else uuid4().hex)
        await start_run(
            workspace_id=ctx.workspace_id, run_id=run.run_id, kind="audio", title=title, steps_total=total,
        )
        try:
            yield run
        finally:
            await end_run(ctx.workspace_id, run.run_id)

    return _dep


class RunProgress(BaseModel):
    stage: str
    steps_done: int
    steps_total: Optional[int] = None
    title: str = ""


@router.get("/runs/{run_id}", response_model=RunProgress)
async def get_audio_run(run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> RunProgress:
    """Where an in-flight operation is. 404 once it has finished."""
    run = await get_run(ctx.workspace_id, run_id) if _RUN_ID.match(run_id) else None
    if not run:
        raise HTTPException(status_code=404, detail="No run in progress with that id.")
    return RunProgress(
        stage=run.get("stage", ""), steps_done=int(run.get("steps_done") or 0),
        steps_total=run.get("steps_total"), title=run.get("title", ""),
    )


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
        transcript_snapshot=[w.model_dump() for w in asset.transcript],
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
    # Speaking pace in words a minute (100 to 200). Left out, the member's saved
    # narration speed and the standard pace are used.
    words_per_minute: Optional[int] = Field(None, ge=spoken_length.MIN_WORDS_PER_MINUTE, le=spoken_length.MAX_WORDS_PER_MINUTE)


class LengthPreviewRequest(BaseModel):
    script: str = ""
    # A length the member chose (seconds). Left out, one written in the script is used if there is one.
    target_seconds: Optional[int] = Field(None, ge=1, le=36000)
    words_per_minute: Optional[int] = Field(None, ge=1, le=1000)


class FitScriptRequest(BaseModel):
    script: str = Field(..., min_length=1, max_length=script_fitting.MAX_SCRIPT_CHARS)
    target_seconds: int = Field(..., ge=1, le=36000)
    words_per_minute: Optional[int] = Field(None, ge=1, le=1000)


@router.post("/fit-script")
@limiter.limit("10/minute")
async def fit_script(
    request: Request,
    body: FitScriptRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict:
    """Rewrites a script to run about the chosen length (Expand to fit, Trim to fit). Nothing is
    saved and no narration is made: the member reads the result and keeps it or not. One AI call."""
    await assert_ai_budget_available(ctx.workspace_id)
    set_usage_workspace(ctx.workspace_id)
    language = await workspace_language(ctx.workspace_id) or "en"

    async def ask(prompt: str) -> str:
        return await call_llm(prompt, temperature=0.5, max_tokens=4000)

    return await script_fitting.fit_script(
        body.script, body.target_seconds, body.words_per_minute, resolve_language_directive_name(language), ask,
    )


@router.get("/video-clips")
@limiter.limit("60/minute")
async def list_video_clips(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """Every video made from a recording in this workspace, newest first, each with its playable file and the recording
    it came from. This is the Video pipeline's history."""
    docs = await audio_assets.find(
        {"workspace_id": ctx.workspace_id, "video_clips.0": {"$exists": True}},
        {"_id": 0, "id": 1, "title": 1, "brand_id": 1, "video_clips": 1},
    ).to_list(length=500)
    clips = [
        {"audio_asset_id": d["id"], "audio_title": d.get("title", ""), "brand_id": d.get("brand_id"), **{k: v for k, v in dict(c).items()}}
        for d in docs for c in (d.get("video_clips") or [])
    ]
    clips.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
    total = len(clips)
    clips = clips[skip:skip + limit]
    media_ids = [c["media_id"] for c in clips if c.get("media_id")]
    media_by_id: dict = {}
    if media_ids:
        found = await media_assets.find({"id": {"$in": media_ids}, "workspace_id": ctx.workspace_id}, {"_id": 0}).to_list(length=len(media_ids))
        media_by_id = {m["id"]: m for m in found}
    return {"items": [{**c, "media": media_by_id.get(c.get("media_id"))} for c in clips], "total": total}


@router.get("/video-presets")
@limiter.limit("60/minute")
async def video_presets_list(request: Request, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict:
    """Where a video is usually posted and what works there (best shape, longest length, length that holds attention)."""
    return {"presets": video_presets.presets_payload(), "note": video_presets.DISCLAIMER}


@router.post("/length-preview")
@limiter.limit("60/minute")
async def length_preview(
    request: Request,
    body: LengthPreviewRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict:
    """How long this script will run, before any narration quota is spent.

    Returns the estimate, the target (the member's choice, or a length written in
    the script such as "a 2 minute intro"), whether the script is short, long or
    about right for that target, and the allowed range. No provider is called.
    """
    detected = spoken_length.detect_requested_seconds(body.script)
    target = spoken_length.clamp_target_seconds(body.target_seconds) if body.target_seconds else detected
    result: dict = {
        "estimated_seconds": spoken_length.estimate_seconds(body.script, body.words_per_minute),
        "words": spoken_length.count_words(body.script),
        "words_per_minute": spoken_length.clamp_words_per_minute(body.words_per_minute),
        "detected_seconds": detected,
        "target_seconds": target,
        "fit": spoken_length.fit_assessment(body.script, target, body.words_per_minute) if target else None,
        "min_seconds": spoken_length.MIN_TARGET_SECONDS,
        "max_seconds": spoken_length.MAX_TARGET_SECONDS,
        "presets_seconds": list(spoken_length.PRESETS_SECONDS),
    }
    return result


@router.post("/generate", response_model=AudioAsset, status_code=201)
@limiter.limit("10/minute")
async def generate_audio_asset(
    request: Request,
    body: GenerateAudioAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
    run: AudioRun = Depends(audio_run("Narration", 2)),
) -> AudioAsset:
    """Real script -> TTS -> AudioAsset. `source_piece_id` (file 04 Part 1):
    when given and `script` is omitted, uses that piece's real content
    directly as the script — the plan's own "optionally passed through a
    short reformatting pass" is a real Phase-2 enhancement, deliberately
    skipped here to keep this slice minimal but real, not faked.
    """
    return await create_audio_from_script(body, ctx, run)


async def create_audio_from_script(body: GenerateAudioAssetRequest, ctx: WorkspaceContext, run) -> AudioAsset:
    """The work behind POST /generate, callable without a request (campaign runs use it).
    `run` only needs an async `step(label)`."""
    await assert_generation_allowed(ctx.workspace_id)
    await run.step("Voicing your script")

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
    if body.words_per_minute:
        # The pace control is real: 150 words a minute is normal speed, and the provider
        # accepts 0.8x to 1.5x, so the chosen pace maps onto that range.
        voice_settings = voice_settings.model_copy(update={
            "speech_speed": round(min(1.5, max(0.8, body.words_per_minute / spoken_length.DEFAULT_WORDS_PER_MINUTE)), 2)
        })
    lexicon = await _get_lexicon(ctx.workspace_id, ctx.user_id)
    speech = await synthesize_speech_timed(
        text=script,
        voice_settings=voice_settings,
        lexicon=lexicon,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
    )
    if not speech:
        raise HTTPException(
            status_code=503,
            detail=(
                "Real narration couldn't be synthesized right now — no ElevenLabs "
                "key configured, no real voice set up yet, or the provider call "
                "itself failed. Nothing was created."
            ),
        )
    audio_bytes = speech.audio

    await run.step("Saving the audio")
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
        size_bytes=len(audio_bytes),
        # The real length: from the audio file itself, else from the provider's word timings.
        # Never the browser's guess, which is what made every recording read as a fixed length.
        duration_s=spoken_length.audio_duration_seconds(audio_bytes) or spoken_length.duration_from_words(speech.words or []),
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
        # Real per-word timing from the provider itself when it gave one
        # (ElevenLabs does; the Deepgram fallback doesn't) — not from Whisper,
        # since the words spoken are already known exactly.
        transcript=speech.words or [],
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
    run: AudioRun = Depends(audio_run("Dialogue", 3)),
) -> AudioAsset:
    """Synthesizes each turn (member's own voice when no voice_id is
    given, the turn's real voice_id otherwise via an ad-hoc
    MemberVoiceSettings — synthesize_speech needs no second code path for
    a non-member voice), then stitches them into one real continuous
    file via concatenate_turns. A named guest speaker's voice_id is saved
    as a real GuestVoiceProfile, scoped to this one AudioAsset, so the
    same guest can be reused across turns without repeating the id."""
    await assert_generation_allowed(ctx.workspace_id)
    await run.step("Voicing each turn")

    if not body.turns:
        raise HTTPException(status_code=400, detail="At least one turn is required.")

    member_voice_settings_obj = await _get_voice_settings(ctx.workspace_id, ctx.user_id)
    member_lexicon_obj = await _get_lexicon(ctx.workspace_id, ctx.user_id)

    turn_audio: list[bytes] = []
    guest_voices_seen: dict[str, str] = {}
    transcript: list[TranscriptWord] = []
    offset_s = 0.0
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

        speech = await synthesize_speech_timed(
            text=turn.text, voice_settings=voice_settings, lexicon=turn_lexicon,
            workspace_id=ctx.workspace_id, user_id=ctx.user_id,
        )
        if not speech:
            raise HTTPException(
                status_code=503,
                detail=f"Couldn't synthesize the turn for '{turn.speaker}'. No real voice or provider is available.",
            )
        turn_audio.append(speech.audio)
        for w in speech.words or []:
            transcript.append(TranscriptWord(
                word=w.word, start_s=round(w.start_s + offset_s, 3), end_s=round(w.end_s + offset_s, 3),
                speaker=turn.speaker,
            ))
        # This turn's own real length (before concatenate_turns' own resample
        # to the first turn's rate, which never changes duration) is what the
        # next turn's words need added to their timing.
        turn_samples, turn_sr = sf.read(io.BytesIO(speech.audio))
        offset_s += len(turn_samples) / turn_sr

    try:
        await run.step("Joining the turns together")
        combined_bytes = concatenate_turns(turn_audio)
    except Exception as exc:  # noqa: BLE001
        logger.error("Dialogue stitching failed for workspace %s: %s", ctx.workspace_id, exc)
        raise HTTPException(status_code=502, detail="Couldn't combine the dialogue turns into one file.")

    await run.step("Saving the audio")
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
        size_bytes=len(combined_bytes),
        duration_s=spoken_length.audio_duration_seconds(combined_bytes),
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
        # Only really timed when every turn used the member's own or a real
        # provider voice_id with timing support; a turn synthesized via the
        # Deepgram fallback has no timing to offer, so it's honestly skipped
        # rather than guessed — the rest of the transcript is still real.
        transcript=transcript,
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
        candidate = clean_translation(await call_llm(translate_prompt))
        if not candidate:
            continue
        if looks_leaked(candidate, source_text):
            logger.warning(
                "Translation attempt %d/%d for target_language=%s echoed prompt text - discarded",
                attempt, _TRANSLATION_MAX_ATTEMPTS, target_language,
            )
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
    speech = await synthesize_speech_timed(
        text=translated_text, voice_settings=voice_settings, lexicon=lexicon,
        workspace_id=ctx.workspace_id, user_id=ctx.user_id,
    )
    if not speech:
        raise HTTPException(
            status_code=503,
            detail="Translation succeeded but narration couldn't be synthesized right now.",
        )
    audio_bytes = speech.audio

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
        size_bytes=len(audio_bytes),
        duration_s=spoken_length.audio_duration_seconds(audio_bytes),
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
        transcript=speech.words or [],
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


def _base_mime_type(content_type: str) -> str:
    """Strips codec parameters a browser's own MediaRecorder adds (real mic
    recordings arrive as "audio/webm;codecs=opus", not bare "audio/webm")
    so the real file type still matches the allow-list."""
    return (content_type or "").split(";")[0].strip().lower()


@router.post("/upload", response_model=AudioAsset, status_code=201)
@limiter.limit("20/minute")
async def upload_audio_asset(
    request: Request,
    title: str = Form(...),
    brand_id: str = Form(...),
    file: UploadFile = File(...),
    ctx: WorkspaceContext = Depends(require("create_content")),
    run: AudioRun = Depends(audio_run("Uploading audio", 4)),
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
    content_type = _base_mime_type(file.content_type or "")
    if content_type not in _AUDIO_MIME_TYPES:
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

    return await _ingest_audio(
        contents=contents, content_type=content_type, filename=file.filename or "audio.mp3",
        title=title, brand_id=brand_id, ctx=ctx, run=run,
    )


async def _ingest_audio(
    *, contents: bytes, content_type: str, filename: str, title: str, brand_id: str, ctx: WorkspaceContext,
    run: Optional[AudioRun] = None,
) -> AudioAsset:
    """Everything after the bytes are in hand: cleanup, storage, the kept
    original, transcription, the asset and its first version. Shared by the
    file upload and the link import so both behave identically."""
    if run:
        await run.step("Cleaning up the audio")
    dsp_settings: dict = {}
    media_bytes = contents
    media_mime = content_type
    if is_dsp_supported(content_type):
        try:
            media_bytes, dsp_settings = enhance_audio(contents, content_type)
            media_mime = "audio/wav"
        except Exception as exc:  # noqa: BLE001
            logger.warning("DSP enhancement failed, using original upload: %s", exc)
            media_bytes, media_mime, dsp_settings = contents, content_type, {}

    if run:
        await run.step("Saving the audio")
    try:
        url = await upload_file(media_bytes, UploadContentType.AUDIO, ctx.user_id)
    except Exception as exc:  # noqa: BLE001 — same tolerance as media.py's own upload_media
        logger.error("Audio upload failed for workspace %s: %s", ctx.workspace_id, exc)
        raise HTTPException(status_code=502, detail="Audio upload failed. Try again.")

    if run:
        await run.step("Transcribing what was said")
    transcript = await transcribe_audio_bytes(contents, filename=filename)

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
        size_bytes=len(media_bytes),
        duration_s=spoken_length.audio_duration_seconds(media_bytes),
    )
    await media_assets.insert_one(media.model_dump())

    # Keep the untouched original when cleanup changed the file, so the A/B
    # comparison has a real "before". Best effort: a failure here never
    # blocks the upload, the comparison is just unavailable for this one.
    original_media_id: Optional[str] = None
    if dsp_settings:
        try:
            original_url = await upload_file(contents, UploadContentType.AUDIO, ctx.user_id)
            original = MediaAsset(
                id=str(uuid4()),
                workspace_id=ctx.workspace_id,
                kind=MediaKind.AUDIO,
                url=original_url,
                mime_type=content_type or "audio/mpeg",
                source=MediaSource.UPLOADED,
                created_by=ctx.user_id,
                created_at=now,
                size_bytes=len(contents),
            )
            await media_assets.insert_one(original.model_dump())
            original_media_id = original.id
        except Exception as exc:  # noqa: BLE001
            logger.warning("Couldn't keep the original upload for A/B comparison: %s", exc)

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
        original_media_id=original_media_id,
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
    asset_id: str, workspace_id: str, new_media_id: str, script_snapshot: Optional[str], action: str, actor_user_id: str,
    *, transcript: Optional[list[dict]] = None, extra_set: Optional[dict] = None,
) -> Optional[dict]:
    """Records a new version. `transcript`, when given, replaces the asset's
    word timings (cleanup that cuts time moves them, and restoring an earlier
    version brings its own back); otherwise they are left alone."""
    doc = await audio_assets.find_one({"id": asset_id, "workspace_id": workspace_id})
    if not doc:
        return None
    new_version_number = doc.get("version_count", 1) + 1
    now = datetime.now(timezone.utc)
    changes = {"media_id": new_media_id, "version_count": new_version_number, "updated_at": now, **(extra_set or {})}
    if transcript is not None:
        changes["transcript"] = transcript
    await audio_assets.update_one(
        {"id": asset_id, "workspace_id": workspace_id},
        {"$set": changes},
    )
    version = AudioAssetVersion(
        version_id=str(uuid4()),
        audio_asset_id=asset_id,
        workspace_id=workspace_id,
        user_id=actor_user_id,
        version_number=new_version_number,
        script_snapshot=script_snapshot,
        media_id=new_media_id,
        transcript_snapshot=transcript if transcript is not None else doc.get("transcript", []),
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
            "brand_id": d.get("brand_id"),
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
        transcript=version_doc.get("transcript_snapshot"),
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


class AudioShareLinkItem(BaseModel):
    token: str
    url: str
    created_at: datetime
    expires_at: datetime
    allow_comments: bool = False
    hold_for_approval: bool = True
    # Real, anonymous, deduped per visitor per day — see app.api.v1.share.
    views: int = 0
    plays: int = 0
    completed_25: int = 0
    completed_50: int = 0
    completed_75: int = 0
    completed_100: int = 0
    pending_comments: int = 0


class AudioShareLinkUpdate(BaseModel):
    allow_comments: Optional[bool] = None
    hold_for_approval: Optional[bool] = None


async def _share_link_item(d: dict, audio_asset_id: str, workspace_id: str) -> AudioShareLinkItem:
    counts = {row["_id"]: row["n"] for row in await share_view_events.aggregate([
        {"$match": {"token": d["token"]}},
        {"$group": {"_id": "$event_type", "n": {"$sum": 1}}},
    ]).to_list(length=10)}
    pending = await audio_comments.count_documents({
        "audio_asset_id": audio_asset_id, "workspace_id": workspace_id, "is_guest": True, "approved": False,
    })
    return AudioShareLinkItem(
        token=d["token"], url=f"{settings.FRONTEND_URL}/share/{d['token']}",
        created_at=d["created_at"], expires_at=d["expires_at"],
        allow_comments=bool(d.get("allow_comments")), hold_for_approval=d.get("hold_for_approval", True),
        views=counts.get("view", 0), plays=counts.get("play", 0),
        completed_25=counts.get("25", 0), completed_50=counts.get("50", 0),
        completed_75=counts.get("75", 0), completed_100=counts.get("100", 0),
        pending_comments=pending,
    )


@router.get("/{audio_asset_id}/share-links", response_model=list[AudioShareLinkItem])
async def list_audio_share_links(
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> list[AudioShareLinkItem]:
    """This recording's links that still work — not revoked and not expired."""
    if not await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    now = datetime.now(timezone.utc)
    docs = await audio_share_links.find(
        {"audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id, "revoked": False},
    ).sort("created_at", -1).to_list(length=100)
    items = []
    for d in docs:
        expires = d["expires_at"] if d["expires_at"].tzinfo else d["expires_at"].replace(tzinfo=timezone.utc)
        if expires <= now:
            continue
        items.append(await _share_link_item(d, audio_asset_id, ctx.workspace_id))
    return items


@router.patch("/{audio_asset_id}/share-link/{token}", response_model=AudioShareLinkItem)
@limiter.limit("30/minute")
async def update_audio_share_link(
    request: Request,
    audio_asset_id: str,
    token: str,
    body: AudioShareLinkUpdate,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioShareLinkItem:
    """Turns guest comments on or off for this one link, and whether a
    guest note needs the owner's approval before anyone else sees it."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    result = await audio_share_links.find_one_and_update(
        {"token": token, "audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": updates}, return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Share link not found.")
    return await _share_link_item(result, audio_asset_id, ctx.workspace_id)


@router.delete("/{audio_asset_id}/share-link/{token}", status_code=204)
async def revoke_audio_share_link(
    audio_asset_id: str,
    token: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> Response:
    """Stops a link working immediately. The link record stays (revoked) so
    the history of what was shared isn't lost."""
    result = await audio_share_links.update_one(
        {"token": token, "audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {"revoked": True}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Share link not found.")
    return Response(status_code=204)


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


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def _media_size_bytes(media: dict) -> int:
    """The real file size. Cached at creation time for anything made after
    this existed; for older files, one real HEAD request, cached back onto
    the media doc so it's asked only once."""
    if media.get("size_bytes"):
        return int(media["size_bytes"])
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.head(media["url"], follow_redirects=True)
            size = int(res.headers.get("content-length") or 0)
    except (httpx.HTTPError, ValueError):
        return 0
    if size:
        await media_assets.update_one({"id": media["id"]}, {"$set": {"size_bytes": size}})
    return size


async def _feed_checklist(doc: dict, episode_count: int, cover_url: Optional[str]) -> list[PodcastFeedChecklistItem]:
    """Real, live-checked conditions — Spotify for Podcasters and Apple
    Podcasts Connect both reject a feed missing any of these."""
    return [
        PodcastFeedChecklistItem(label="Title", ok=bool((doc.get("title") or "").strip())),
        PodcastFeedChecklistItem(label="Description", ok=bool((doc.get("description") or "").strip()),
                                  hint="Add a description so listeners know what the show is about."),
        PodcastFeedChecklistItem(label="Cover art", ok=bool(cover_url),
                                  hint="Apple requires a square cover image, at least 1400x1400px."),
        PodcastFeedChecklistItem(label="Author", ok=bool((doc.get("author_name") or "").strip()),
                                  hint="Who the show is by, shown in podcast directories."),
        PodcastFeedChecklistItem(
            label="Owner email", ok=bool(_EMAIL_RE.match(doc.get("owner_email") or "")),
            hint="A real email address. Apple sends technical notices here, never shown to listeners.",
        ),
        PodcastFeedChecklistItem(label="Category", ok=(doc.get("category") or "") in PODCAST_CATEGORIES,
                                  hint="Pick one of Apple Podcasts' categories."),
        PodcastFeedChecklistItem(label="Language", ok=bool((doc.get("language") or "").strip())),
        PodcastFeedChecklistItem(label="At least one approved episode", ok=episode_count > 0,
                                  hint="Approve a recording so it appears in the feed."),
    ]


async def _feed_status_response(doc: dict) -> PodcastFeedStatusResponse:
    episode_count = await _feed_episode_count(doc["brand_id"])
    cover_url = None
    if doc.get("cover_media_id"):
        cover = await media_assets.find_one({"id": doc["cover_media_id"]})
        cover_url = cover["url"] if cover else None
    return PodcastFeedStatusResponse(
        is_enabled=doc["is_enabled"], token=doc["token"],
        feed_url=f"{settings.FRONTEND_URL.rstrip('/')}/api/v1/audio-assets/feed/{doc['token']}.xml" if doc["is_enabled"] else None,
        title=doc.get("title", ""), description=doc.get("description", ""), episode_count=episode_count,
        language=doc.get("language", "en"), category=doc.get("category", ""), explicit=doc.get("explicit", False),
        author_name=doc.get("author_name", ""), owner_email=doc.get("owner_email", ""), cover_url=cover_url,
        checklist=await _feed_checklist(doc, episode_count, cover_url),
    )


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
        language=(existing or {}).get("language", "en"), category=(existing or {}).get("category", ""),
        explicit=(existing or {}).get("explicit", False), author_name=(existing or {}).get("author_name", ""),
        owner_email=(existing or {}).get("owner_email", ""), cover_media_id=(existing or {}).get("cover_media_id"),
        created_at=(existing["created_at"] if existing else now), updated_at=now,
    )
    await podcast_feed_settings.update_one(
        {"brand_id": body.brand_id, "workspace_id": ctx.workspace_id},
        {"$set": doc.model_dump()},
        upsert=True,
    )
    updated = await podcast_feed_settings.find_one({"brand_id": body.brand_id, "workspace_id": ctx.workspace_id})
    return await _feed_status_response(updated)


@router.get("/feed/status", response_model=PodcastFeedStatusResponse)
@limiter.limit("30/minute")
async def get_podcast_feed_status(
    request: Request,
    brand_id: str = Query(...),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> PodcastFeedStatusResponse:
    doc = await podcast_feed_settings.find_one({"brand_id": brand_id, "workspace_id": ctx.workspace_id})
    if not doc:
        return PodcastFeedStatusResponse(is_enabled=False, checklist=await _feed_checklist({}, 0, None))
    return await _feed_status_response(doc)


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

    if body.category is not None and body.category not in ("", *PODCAST_CATEGORIES):
        raise HTTPException(status_code=400, detail=f"category must be one of: {', '.join(PODCAST_CATEGORIES)}.")
    if body.owner_email is not None and body.owner_email and not _EMAIL_RE.match(body.owner_email):
        raise HTTPException(status_code=400, detail="owner_email doesn't look like a real email address.")
    if body.cover_media_id is not None and body.cover_media_id:
        cover = await media_assets.find_one({"id": body.cover_media_id, "workspace_id": ctx.workspace_id})
        if not cover or cover.get("kind") != MediaKind.IMAGE.value:
            raise HTTPException(status_code=400, detail="That isn't an image file in this workspace.")

    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    for field in ("title", "description", "is_enabled", "language", "category", "explicit", "author_name", "owner_email", "cover_media_id"):
        value = getattr(body, field)
        if value is not None:
            updates[field] = value
    await podcast_feed_settings.update_one({"brand_id": brand_id, "workspace_id": ctx.workspace_id}, {"$set": updates})

    updated = await podcast_feed_settings.find_one({"brand_id": brand_id, "workspace_id": ctx.workspace_id})
    return await _feed_status_response(updated)


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
        size = await _media_size_bytes(media)
        items_xml.append(f"""    <item>
      <title>{_escape_xml(ep.get('title', 'Untitled episode'))}</title>
      <guid isPermaLink="false">{ep['id']}</guid>
      <pubDate>{pub_date.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>
      <enclosure url="{_escape_xml(media['url'])}" type="{_escape_xml(media.get('mime_type') or 'audio/mpeg')}" length="{size}" />
      <itunes:duration>{int(media.get('duration_s') or 0)}</itunes:duration>
    </item>""")

    title = _escape_xml(settings_doc.get("title") or "Podcast")
    description = _escape_xml(settings_doc.get("description") or "")
    language = _escape_xml(settings_doc.get("language") or "en")
    category = settings_doc.get("category") or ""
    author = _escape_xml(settings_doc.get("author_name") or title)
    owner_email = _escape_xml(settings_doc.get("owner_email") or "")
    explicit = "true" if settings_doc.get("explicit") else "false"

    cover_xml = ""
    if settings_doc.get("cover_media_id"):
        cover = await media_assets.find_one({"id": settings_doc["cover_media_id"]})
        if cover:
            cover_url = _escape_xml(cover["url"])
            cover_xml = f"""    <itunes:image href="{cover_url}" />
    <image>
      <url>{cover_url}</url>
      <title>{title}</title>
      <link>{_escape_xml(settings.FRONTEND_URL)}</link>
    </image>
"""

    category_xml = f'    <itunes:category text="{_escape_xml(category)}" />\n' if category in PODCAST_CATEGORIES else ""
    owner_xml = (
        f"""    <itunes:owner>
      <itunes:name>{author}</itunes:name>
      <itunes:email>{owner_email}</itunes:email>
    </itunes:owner>
"""
        if owner_email else ""
    )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{title}</title>
    <description>{description}</description>
    <language>{language}</language>
    <itunes:explicit>{explicit}</itunes:explicit>
    <itunes:author>{author}</itunes:author>
{owner_xml}{category_xml}{cover_xml}{chr(10).join(items_xml)}
  </channel>
</rss>"""
    return Response(content=xml, media_type="application/rss+xml")


# ─────────────────────────────────────────────────────────────────────────────
# Review comments pinned to a moment in a recording. Anyone in the workspace
# can read them; content creators can add and resolve; only the author or an
# owner/admin can delete.
# ─────────────────────────────────────────────────────────────────────────────

async def _get_asset_or_404(audio_asset_id: str, workspace_id: str) -> dict:
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": workspace_id}, {"id": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    return doc


@router.get("/{audio_asset_id}/comments", response_model=list[AudioComment])
async def list_audio_comments(
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> list[AudioComment]:
    await _get_asset_or_404(audio_asset_id, ctx.workspace_id)
    docs = await audio_comments.find(
        {"audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id},
    ).sort("time_s", 1).to_list(length=500)
    return [AudioComment(**d) for d in docs]


@router.post("/{audio_asset_id}/comments", response_model=AudioComment, status_code=201)
@limiter.limit("60/minute")
async def create_audio_comment(
    request: Request,
    audio_asset_id: str,
    body: AudioCommentCreate,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioComment:
    await _get_asset_or_404(audio_asset_id, ctx.workspace_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write something for the note.")
    if len(text) > 1000:
        raise HTTPException(status_code=400, detail="Keep the note under 1000 characters.")
    if body.time_s < 0:
        raise HTTPException(status_code=400, detail="The time can't be before the start.")

    user = await users.find_one({"id": ctx.user_id}, {"name": 1, "email": 1})
    comment = AudioComment(
        id=uuid4().hex,
        audio_asset_id=audio_asset_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        user_name=((user or {}).get("name") or (user or {}).get("email") or "Member"),
        time_s=body.time_s,
        text=text,
        created_at=datetime.now(timezone.utc),
    )
    await audio_comments.insert_one(comment.model_dump())
    return comment


@router.patch("/{audio_asset_id}/comments/{comment_id}", response_model=AudioComment)
@limiter.limit("60/minute")
async def update_audio_comment(
    request: Request,
    audio_asset_id: str,
    comment_id: str,
    body: AudioCommentUpdate,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioComment:
    updates = {k: v for k, v in (("resolved", body.resolved), ("approved", body.approved)) if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    result = await audio_comments.find_one_and_update(
        {"id": comment_id, "audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": updates},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Comment not found.")
    return AudioComment(**result)


@router.delete("/{audio_asset_id}/comments/{comment_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_audio_comment(
    request: Request,
    audio_asset_id: str,
    comment_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Response:
    doc = await audio_comments.find_one(
        {"id": comment_id, "audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Comment not found.")
    if doc["user_id"] != ctx.user_id and ctx.role not in (WorkspaceRole.OWNER.value, WorkspaceRole.ADMIN.value):
        raise HTTPException(status_code=403, detail="Only the author, or a workspace owner/admin, can delete this note.")
    await audio_comments.delete_one({"id": comment_id})
    return Response(status_code=204)


# ─────────────────────────────────────────────────────────────────────────────
# Import from a link: a direct audio file, or an episode picked from a podcast
# RSS feed. Runs through the same path as a file upload.
# ─────────────────────────────────────────────────────────────────────────────

class ImportFeedRequest(BaseModel):
    url: str


class ImportLinkRequest(BaseModel):
    brand_id: str
    url: str
    title: Optional[str] = None


@router.post("/import/feed", response_model=FeedListing)
@limiter.limit("10/minute")
async def list_import_feed(
    request: Request,
    body: ImportFeedRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> FeedListing:
    """Lists the episodes in a podcast feed so the member can pick one."""
    try:
        return await list_feed_episodes(body.url)
    except LinkFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/import", response_model=AudioAsset, status_code=201)
@limiter.limit("10/minute")
async def import_audio_from_link(
    request: Request,
    body: ImportLinkRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
    run: AudioRun = Depends(audio_run("Importing audio", 5)),
) -> AudioAsset:
    """Fetches an audio file from a public link and creates a recording from
    it exactly as an upload would (cleanup, transcript, kept original)."""
    max_bytes = await _max_bytes_for(MediaKind.AUDIO, ctx.workspace_id)
    await run.step("Fetching the file")
    try:
        contents, mime, filename = await fetch_audio(body.url, max_bytes)
    except LinkFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await _ingest_audio(
        contents=contents, content_type=mime, filename=filename,
        title=(body.title or filename).strip()[:200] or "Imported audio", brand_id=body.brand_id, ctx=ctx, run=run,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup: the dials on the Cleanup tab. Applies what the member chose to the
# current file and records the result as a new, restorable version.
# ─────────────────────────────────────────────────────────────────────────────

async def _download_media_bytes(url: str) -> bytes:
    """The stored file behind a media asset (our own storage, not a user link)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            res = await client.get(url)
            res.raise_for_status()
            return res.content
    except httpx.HTTPError as exc:
        logger.warning("Couldn't load stored audio %s: %s", url, exc)
        raise HTTPException(status_code=502, detail="Couldn't load this recording. Try again.")


@router.post("/{audio_asset_id}/transcribe", response_model=AudioAsset)
@limiter.limit("6/minute")
async def transcribe_audio_asset(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
    run: AudioRun = Depends(audio_run("Transcribing audio", 2)),
) -> AudioAsset:
    """A real Whisper transcription for a recording with no word timing yet
    — narrations voiced through the Deepgram fallback (no timing of its
    own), and anything made before timed synthesis existed. Explicit and
    quota-gated (a real Groq spend) rather than run automatically, since a
    recording can already have a real transcript with nothing to gain."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    if doc.get("transcript"):
        raise HTTPException(status_code=400, detail="This recording already has a transcript.")
    media_id = doc.get("approved_master_media_id") or doc.get("media_id")
    if not media_id:
        raise HTTPException(status_code=400, detail="This recording has no audio file to transcribe.")
    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    if not media:
        raise HTTPException(status_code=400, detail="This recording's audio file couldn't be found.")

    await run.step("Loading the recording")
    audio_bytes = await _download_media_bytes(media["url"])
    await run.step("Transcribing what was said")
    transcript = await transcribe_audio_bytes(audio_bytes, filename=f"{audio_asset_id}.mp3")
    if not transcript:
        raise HTTPException(
            status_code=503,
            detail="Transcription didn't return anything real. The provider call may have failed. Nothing was changed.",
        )

    await audio_assets.update_one(
        {"id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {"transcript": [w.model_dump() for w in transcript], "updated_at": datetime.now(timezone.utc)}},
    )
    updated = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    return AudioAsset(**updated)


@router.post("/{audio_asset_id}/cleanup", response_model=AudioAsset)
@limiter.limit("6/minute")
async def cleanup_audio_asset(
    request: Request,
    audio_asset_id: str,
    body: CleanupSettings,
    ctx: WorkspaceContext = Depends(require("edit_content")),
    run: AudioRun = Depends(audio_run("Cleaning up audio", 3)),
) -> AudioAsset:
    """Builds on the current version, so a second apply stacks on the first;
    restore an earlier version to start over. An approved master is never
    replaced: it stays pinned, and the new version sits beside it."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    if body.is_empty():
        raise HTTPException(status_code=400, detail="Turn on at least one cleanup option first.")
    media = await media_assets.find_one({"id": doc.get("media_id"), "workspace_id": ctx.workspace_id}) if doc.get("media_id") else None
    if not media:
        raise HTTPException(status_code=400, detail="This recording has no audio file to clean up.")

    await run.step("Loading the recording")
    source_bytes = await _download_media_bytes(media["url"])

    echo_note: Optional[str] = None
    if body.remove_echo:
        try:
            source_bytes = await reduce_echo(source_bytes)
        except EchoReductionError as exc:
            # ElevenLabs cannot do it (no key, free plan, or switched off): do the honest basic cleanup instead and say so
            try:
                source_bytes = await basic_cleanup(source_bytes)
                echo_note = BASIC_CLEANUP_NOTE
            except EchoReductionError:
                raise HTTPException(status_code=400, detail=str(exc))

    transcript = [dict(w) for w in doc.get("transcript", [])]
    await run.step("Applying your changes")
    try:
        cleaned, new_transcript, applied = await asyncio.to_thread(apply_cleanup, source_bytes, body, transcript)
    except CleanupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if body.remove_echo:
        applied["remove_echo"] = "basic" if echo_note else True
        if echo_note:
            applied["remove_echo_note"] = echo_note

    await run.step("Saving the new version")
    try:
        url = await upload_file(cleaned, UploadContentType.AUDIO, ctx.user_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Cleaned audio upload failed for %s: %s", audio_asset_id, exc)
        raise HTTPException(status_code=502, detail="Saving the cleaned audio failed. Try again.")

    now = datetime.now(timezone.utc)
    new_media = MediaAsset(
        id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.AUDIO, url=url,
        mime_type="audio/wav", source=MediaSource.ENHANCED, created_by=ctx.user_id, created_at=now,
        size_bytes=len(cleaned),
    )
    await media_assets.insert_one(new_media.model_dump())

    updated = await _bump_audio_version(
        audio_asset_id, ctx.workspace_id, new_media.id, doc.get("script"), "cleanup", ctx.user_id,
        transcript=new_transcript,
        extra_set={"dsp_settings": {**doc.get("dsp_settings", {}), "cleanup": applied}},
    )
    return AudioAsset(**updated)


# ─────────────────────────────────────────────────────────────────────────────
# The kit: a brand's reusable episode pieces (intro and outro clips, a sponsor
# read, music beds), and "assemble", which builds a finished episode from a
# recording plus those pieces into a new file.
# ─────────────────────────────────────────────────────────────────────────────

async def _brand_or_404(brand_id: str, workspace_id: str) -> None:
    if not await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="Brand not found.")


async def _audio_media_or_400(media_id: str, workspace_id: str) -> dict:
    media = await media_assets.find_one({"id": media_id, "workspace_id": workspace_id})
    if not media or media.get("kind") != MediaKind.AUDIO.value:
        raise HTTPException(status_code=400, detail="That file isn't an audio file in this workspace.")
    return media


async def _load_kit(brand_id: str, workspace_id: str) -> AudioKit:
    doc = await audio_kits.find_one({"brand_id": brand_id, "workspace_id": workspace_id})
    return AudioKit(**doc) if doc else AudioKit(brand_id=brand_id, workspace_id=workspace_id)


async def _resolve_music_bed(kit: AudioKit, bed_id: Optional[str]) -> Optional[MusicBed]:
    """Looks up `bed_id` in the brand's own kit first, then the shared
    curated library — both are real MusicBed-shaped records pointing at a
    real media_assets doc, so every downstream byte-fetch stays unchanged."""
    if not bed_id:
        return None
    own = next((b for b in kit.music_beds if b.id == bed_id), None)
    if own:
        return own
    track = next((t for t in await list_library_tracks() if t.id == bed_id), None)
    return MusicBed(id=track.id, name=track.name, media_id=track.media_id) if track else None


async def _kit_out(kit: AudioKit) -> AudioKitOut:
    clips = (kit.intro, kit.outro, kit.sponsor_clip, kit.transition_sfx, kit.brand_signature)
    library = await list_library_tracks()
    ids = [c.media_id for c in clips if c] + [b.media_id for b in kit.music_beds] + [t.media_id for t in library]
    found = await media_assets.find({"id": {"$in": ids}}).to_list(length=len(ids)) if ids else []
    urls = {m["id"]: m["url"] for m in found}

    def clip(c):
        return KitClipOut(**c.model_dump(), url=urls[c.media_id]) if c and c.media_id in urls else None

    own_beds = [MusicBedOut(**b.model_dump(), url=urls[b.media_id]) for b in kit.music_beds if b.media_id in urls]
    library_beds = [
        MusicBedOut(id=t.id, name=t.name, media_id=t.media_id, url=urls[t.media_id], is_library=True, artist=t.artist)
        for t in library if t.media_id in urls
    ]
    return AudioKitOut(
        brand_id=kit.brand_id,
        intro=clip(kit.intro), outro=clip(kit.outro), sponsor_clip=clip(kit.sponsor_clip),
        sponsor_name=kit.sponsor_name, sponsor_script=kit.sponsor_script,
        transition_sfx=clip(kit.transition_sfx), brand_signature=clip(kit.brand_signature),
        music_beds=own_beds + library_beds,
    )


async def _save_kit(kit: AudioKit) -> None:
    kit.updated_at = datetime.now(timezone.utc)
    await audio_kits.update_one(
        {"brand_id": kit.brand_id, "workspace_id": kit.workspace_id}, {"$set": kit.model_dump()}, upsert=True,
    )


@router.get("/kit/{brand_id}", response_model=AudioKitOut)
async def get_audio_kit(brand_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> AudioKitOut:
    await _brand_or_404(brand_id, ctx.workspace_id)
    return await _kit_out(await _load_kit(brand_id, ctx.workspace_id))


@router.put("/kit/{brand_id}", response_model=AudioKitOut)
@limiter.limit("30/minute")
async def update_audio_kit(
    request: Request,
    brand_id: str,
    body: AudioKitUpdate,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioKitOut:
    await _brand_or_404(brand_id, ctx.workspace_id)
    kit = await _load_kit(brand_id, ctx.workspace_id)
    for field in body.model_fields_set:
        value = getattr(body, field)
        if field in ("intro", "outro", "sponsor_clip", "transition_sfx"):
            if value is not None:
                await _audio_media_or_400(value.media_id, ctx.workspace_id)
            setattr(kit, field, value)
        elif field == "brand_signature":
            if value is not None:
                media = await _audio_media_or_400(value.media_id, ctx.workspace_id)
                duration = media.get("duration_s")
                if duration and duration > 5.0:
                    raise HTTPException(status_code=400, detail="A brand signature has to be 5 seconds or shorter.")
            kit.brand_signature = value
        elif field == "sponsor_name":
            kit.sponsor_name = (value or "").strip()[:120]
        elif field == "sponsor_script":
            script = (value or "").strip()
            if len(script) > 3000:
                raise HTTPException(status_code=400, detail="Keep the sponsor script under 3000 characters.")
            kit.sponsor_script = script
    await _save_kit(kit)
    return await _kit_out(kit)


@router.post("/kit/{brand_id}/beds", response_model=AudioKitOut, status_code=201)
@limiter.limit("30/minute")
async def add_music_bed(
    request: Request,
    brand_id: str,
    body: MusicBedCreate,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioKitOut:
    await _brand_or_404(brand_id, ctx.workspace_id)
    await _audio_media_or_400(body.media_id, ctx.workspace_id)
    name = body.name.strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Give the music bed a name.")
    kit = await _load_kit(brand_id, ctx.workspace_id)
    if len(kit.music_beds) >= 20:
        raise HTTPException(status_code=400, detail="You can keep up to 20 music beds. Remove one first.")
    kit.music_beds.append(MusicBed(id=uuid4().hex, name=name, media_id=body.media_id))
    await _save_kit(kit)
    return await _kit_out(kit)


@router.delete("/kit/{brand_id}/beds/{bed_id}", response_model=AudioKitOut)
@limiter.limit("30/minute")
async def remove_music_bed(
    request: Request,
    brand_id: str,
    bed_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioKitOut:
    await _brand_or_404(brand_id, ctx.workspace_id)
    kit = await _load_kit(brand_id, ctx.workspace_id)
    if not any(b.id == bed_id for b in kit.music_beds):
        raise HTTPException(status_code=404, detail="Music bed not found.")
    kit.music_beds = [b for b in kit.music_beds if b.id != bed_id]
    await _save_kit(kit)
    return await _kit_out(kit)


@router.post("/kit/{brand_id}/sponsor/synthesize", response_model=AudioKitOut)
@limiter.limit("10/minute")
async def synthesize_sponsor_read(
    request: Request,
    brand_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> AudioKitOut:
    """Voices the saved sponsor script in the caller's own voice and stores it
    as the sponsor read."""
    await _brand_or_404(brand_id, ctx.workspace_id)
    await assert_generation_allowed(ctx.workspace_id)
    kit = await _load_kit(brand_id, ctx.workspace_id)
    if not kit.sponsor_script:
        raise HTTPException(status_code=400, detail="Write the sponsor script first.")

    voice_settings = await _get_voice_settings(ctx.workspace_id, ctx.user_id)
    lexicon = await _get_lexicon(ctx.workspace_id, ctx.user_id)
    audio_bytes = await synthesize_speech(
        text=kit.sponsor_script, voice_settings=voice_settings, lexicon=lexicon,
        workspace_id=ctx.workspace_id, user_id=ctx.user_id,
    )
    if not audio_bytes:
        raise HTTPException(status_code=503, detail="The sponsor read couldn't be voiced right now. Nothing was saved.")

    url = await upload_file(audio_bytes, UploadContentType.AUDIO, ctx.user_id)
    media = MediaAsset(
        id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.AUDIO, url=url,
        mime_type="audio/mpeg", source=MediaSource.SYNTHESIZED, created_by=ctx.user_id,
        created_at=datetime.now(timezone.utc), size_bytes=len(audio_bytes),
    )
    await media_assets.insert_one(media.model_dump())
    kit.sponsor_clip = KitClip(media_id=media.id, name=f"{kit.sponsor_name or 'Sponsor'} (voiced)")
    await _save_kit(kit)
    return await _kit_out(kit)


@router.post("/{audio_asset_id}/assemble", response_model=AudioAsset)
@limiter.limit("6/minute")
async def assemble_audio_asset(
    request: Request,
    audio_asset_id: str,
    body: AssemblePlan,
    ctx: WorkspaceContext = Depends(require("edit_content")),
    run: AudioRun = Depends(audio_run("Building episode", 3)),
) -> AudioAsset:
    """Builds a finished episode into a new file. The recording, its versions
    and its approved master are not touched; the result is listed under
    `assemblies`."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    if not (
        body.use_intro or body.use_outro or body.sponsor_at_s is not None or body.music_bed_id
        or body.use_transition_sfx or body.use_brand_signature
    ):
        raise HTTPException(status_code=400, detail="Choose at least one thing to add to the episode.")

    voice_media_id = doc.get("approved_master_media_id") or doc.get("media_id")
    voice = await media_assets.find_one({"id": voice_media_id, "workspace_id": ctx.workspace_id}) if voice_media_id else None
    if not voice:
        raise HTTPException(status_code=400, detail="This recording has no audio file to build on.")

    kit = await _load_kit(doc["brand_id"], ctx.workspace_id)
    bed = await _resolve_music_bed(kit, body.music_bed_id)
    if body.music_bed_id and not bed:
        raise HTTPException(status_code=400, detail="That music bed couldn't be found.")

    async def _clip_bytes(clip, wanted: bool, what: str) -> Optional[bytes]:
        if not wanted:
            return None
        if not clip:
            raise HTTPException(status_code=400, detail=f"There's no {what} set up yet.")
        media = await media_assets.find_one({"id": clip.media_id, "workspace_id": ctx.workspace_id})
        if not media:
            raise HTTPException(status_code=400, detail=f"The {what} file is missing. Set it up again.")
        return await _download_media_bytes(media["url"])

    await run.step("Loading the pieces")
    voice_bytes = await _download_media_bytes(voice["url"])
    intro_bytes = await _clip_bytes(kit.intro, body.use_intro, "intro clip")
    outro_bytes = await _clip_bytes(kit.outro, body.use_outro, "outro clip")
    sponsor_bytes = await _clip_bytes(kit.sponsor_clip, body.sponsor_at_s is not None, "sponsor read")
    bed_bytes = await _clip_bytes(bed, True, "music bed") if bed else None
    transition_bytes = await _clip_bytes(kit.transition_sfx, body.use_transition_sfx, "transition sound")
    signature_bytes = await _clip_bytes(kit.brand_signature, body.use_brand_signature, "brand signature")

    await run.step("Mixing the episode")
    try:
        wav, mixed = await asyncio.to_thread(
            assemble_episode, voice_bytes=voice_bytes, plan=body, intro_bytes=intro_bytes,
            outro_bytes=outro_bytes, sponsor_bytes=sponsor_bytes, bed_bytes=bed_bytes,
            transition_sfx_bytes=transition_bytes, signature_bytes=signature_bytes,
            transcript=doc.get("transcript", []),
        )
    except AssembleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await run.step("Saving the episode")
    try:
        url = await upload_file(wav, UploadContentType.AUDIO, ctx.user_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Assembled episode upload failed for %s: %s", audio_asset_id, exc)
        raise HTTPException(status_code=502, detail="Saving the finished episode failed. Try again.")

    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.AUDIO, url=url,
        mime_type="audio/wav", source=MediaSource.ENHANCED, created_by=ctx.user_id, created_at=now,
        size_bytes=len(wav),
    )
    await media_assets.insert_one(media.model_dump())

    components = {
        **mixed,
        **({"intro_name": kit.intro.name} if body.use_intro and kit.intro else {}),
        **({"outro_name": kit.outro.name} if body.use_outro and kit.outro else {}),
        **({"sponsor_name": kit.sponsor_name or (kit.sponsor_clip.name if kit.sponsor_clip else "Sponsor")}
           if body.sponsor_at_s is not None else {}),
        **({"music_bed_name": bed.name} if bed else {}),
        **({"transition_sfx_name": kit.transition_sfx.name} if body.use_transition_sfx and kit.transition_sfx else {}),
        **({"brand_signature_name": kit.brand_signature.name} if body.use_brand_signature and kit.brand_signature else {}),
    }
    # The source's real transcript, shifted for whatever assembly put before or
    # inside it. Assembly only ever prepends an intro and/or splices a sponsor
    # read INTO the voice — the inverse of cleanup's own cut-and-shift, which
    # removes time instead of adding it.
    prepend_shift = (
        (mixed.get("intro_seconds", 0.0) if body.use_intro else 0.0)
        + (mixed.get("brand_signature_seconds", 0.0) if body.use_brand_signature else 0.0)
    )
    # Every point that pushed later words forward: the sponsor read at its
    # one real position, and each transition sound at its own real pause.
    insertion_points = list(mixed.get("transition_times_s", []))
    transition_seconds = mixed.get("transition_seconds", 0.0)
    if body.sponsor_at_s is not None:
        insertion_points.append(body.sponsor_at_s)
    insertion_lengths = {**{t: transition_seconds for t in mixed.get("transition_times_s", [])}}
    if body.sponsor_at_s is not None:
        insertion_lengths[body.sponsor_at_s] = mixed.get("sponsor_seconds", 0.0)

    def _shift(original_t: float) -> float:
        return prepend_shift + sum(length for at, length in insertion_lengths.items() if at <= original_t)

    assembled_transcript = [
        TranscriptWord(
            word=w["word"] if isinstance(w, dict) else w.word,
            start_s=round(_shift(w["start_s"] if isinstance(w, dict) else w.start_s) + (w["start_s"] if isinstance(w, dict) else w.start_s), 3),
            end_s=round(_shift(w["end_s"] if isinstance(w, dict) else w.end_s) + (w["end_s"] if isinstance(w, dict) else w.end_s), 3),
            speaker=w["speaker"] if isinstance(w, dict) else w.speaker,
        )
        for w in doc.get("transcript", [])
    ]

    assembly = Assembly(
        id=uuid4().hex, media_id=media.id, components=components, transcript=assembled_transcript,
        created_by=ctx.user_id, created_at=now,
    )
    await audio_assets.update_one(
        {"id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$push": {"assemblies": {"$each": [assembly.model_dump()], "$slice": -10}}, "$set": {"updated_at": now}},
    )
    updated = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    return AudioAsset(**updated)


# ─────────────────────────────────────────────────────────────────────────────
# Audio to video: a clip or the whole episode as an mp4, with real
# word-highlighted captions from the real transcript. Renders in the
# background via the same live-step tracker as cleanup/assemble.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{audio_asset_id}/video", response_model=AudioAsset)
@limiter.limit("6/minute")
async def make_video_from_audio_asset(
    request: Request,
    audio_asset_id: str,
    body: MakeVideoRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
    run: AudioRun = Depends(audio_run("Rendering video", 3)),
) -> AudioAsset:
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    if body.style not in ("cover", "solid", "waveform", "cover_wave"):
        raise HTTPException(status_code=400, detail="style must be one of: cover, solid, waveform, cover_wave.")
    if body.size not in ("square", "vertical", "landscape", "portrait"):
        raise HTTPException(status_code=400, detail="size must be one of: square, vertical, landscape, portrait.")
    if body.platform is not None and body.platform not in video_presets.PRESETS:
        raise HTTPException(status_code=400, detail="platform must be one of: " + ", ".join(video_presets.PRESETS) + ".")

    voice_media_id = doc.get("approved_master_media_id") or doc.get("media_id")
    voice = await media_assets.find_one({"id": voice_media_id, "workspace_id": ctx.workspace_id}) if voice_media_id else None
    if not voice:
        raise HTTPException(status_code=400, detail="This recording has no audio file to turn into a video.")

    end_s = body.end_s if body.end_s is not None else float(voice.get("duration_s") or 0)
    if end_s <= body.start_s:
        raise HTTPException(status_code=400, detail="This recording's real length isn't known yet, or the end time given isn't after the start time.")

    brand = await brand_profiles.find_one({"id": doc["brand_id"], "workspace_id": ctx.workspace_id})
    visual_identity = (brand or {}).get("visual_identity") or {}
    colors = visual_identity.get("colors") or {}

    # brand assets go on by default: the logo when the brand has one, the title, the brand colours and font
    from app.api.v1.image_assets import _fetch_logo_bytes
    from app.pipelines.media.headline import trim_headline

    use_logo = body.show_logo if body.show_logo is not None else True
    logo_bytes = await _fetch_logo_bytes(visual_identity.get("logo_url") or "") if use_logo else None
    on_screen_title = trim_headline(body.title or doc.get("title", ""), max_words=10, max_chars=64) if body.show_title else ""

    cover_bytes = None
    if body.style in ("cover", "cover_wave"):
        feed = await podcast_feed_settings.find_one({"brand_id": doc["brand_id"], "workspace_id": ctx.workspace_id})
        cover_media_id = (feed or {}).get("cover_media_id")
        if cover_media_id:
            cover_media = await media_assets.find_one({"id": cover_media_id, "workspace_id": ctx.workspace_id})
            if cover_media:
                cover_bytes = await _download_media_bytes(cover_media["url"])

    await run.step("Loading the recording")
    audio_bytes = await _download_media_bytes(voice["url"])
    words = [TranscriptWordLike(word=w["word"], start_s=w["start_s"], end_s=w["end_s"]) for w in doc.get("transcript", [])]

    await run.step("Rendering the video")
    try:
        video_bytes = await render_video(
            audio_bytes=audio_bytes, words=words, start_s=body.start_s, end_s=end_s,
            style=body.style, size=body.size,
            background_hex=colors.get("secondary") or "", accent_hex=colors.get("accent") or "",
            cover_bytes=cover_bytes, font_name=(visual_identity.get("fonts") or {}).get("heading") or None,
            primary_hex=colors.get("primary") or "", title=on_screen_title, logo_bytes=logo_bytes, progress=body.show_progress,
        )
    except VideoRenderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    max_bytes = await _max_bytes_for(MediaKind.VIDEO, ctx.workspace_id)
    if len(video_bytes) > max_bytes:
        raise HTTPException(status_code=400, detail=f"The rendered video is larger than the {max_bytes // (1024 * 1024)}MB limit. Try a shorter clip.")

    await run.step("Saving the video")
    try:
        url = await upload_file(video_bytes, UploadContentType.VIDEO, ctx.user_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Video render upload failed for %s: %s", audio_asset_id, exc)
        raise HTTPException(status_code=502, detail="Saving the finished video failed. Try again.")

    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.VIDEO, url=url,
        mime_type="video/mp4", source=MediaSource.ENHANCED, created_by=ctx.user_id, created_at=now,
        size_bytes=len(video_bytes), duration_s=round(end_s - body.start_s, 2),
    )
    await media_assets.insert_one(media.model_dump())

    clip = VideoClip(
        id=uuid4().hex, media_id=media.id, start_s=body.start_s, end_s=end_s,
        style=body.style, size=body.size, title=(body.title or doc.get("title", ""))[:200],
        platform=body.platform, notes=video_presets.advice(body.platform, body.size, end_s - body.start_s),
        created_by=ctx.user_id, created_at=now,
    )
    await audio_assets.update_one(
        {"id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$push": {"video_clips": {"$each": [clip.model_dump()], "$slice": -20}}, "$set": {"updated_at": now}},
    )
    updated = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    return AudioAsset(**updated)


async def _generate_clip_suggestions(doc: dict, workspace_id: str, audio_asset_id: str) -> list[SuggestedClip]:
    """One real, gated LLM call over the real transcript, proposing a few
    quotable spans. Shared by /suggest-clips (review-only) and /soundbites
    (which actually extracts them) — same real logic, not duplicated."""
    words = doc.get("transcript", [])
    if not words:
        raise HTTPException(status_code=400, detail="This recording has no transcript to find moments in yet.")

    await assert_ai_budget_available(workspace_id)
    set_usage_workspace(workspace_id)

    prompt = load_prompt("audio/suggest_clips", words=[{"word": w["word"], "start_s": w["start_s"]} for w in words])
    try:
        result = await call_llm_structured(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Clip suggestion failed for %s: %s", audio_asset_id, exc)
        raise HTTPException(status_code=502, detail="Couldn't come up with suggestions right now. Try again.")

    duration = max((w["end_s"] for w in words), default=0.0)
    suggestions = []
    for item in (result.get("suggestions") or [])[:5]:
        try:
            start_s, end_s = float(item["start_s"]), float(item["end_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= start_s < end_s <= duration + 1):
            continue  # a made-up or out-of-range span never reaches the member
        suggestions.append(SuggestedClip(
            start_s=start_s, end_s=end_s,
            quote=str(item.get("quote", ""))[:400], reason=str(item.get("reason", ""))[:200],
        ))
    return suggestions


class ChaptersResponse(BaseModel):
    chapters: list[dict]
    # Why there are none, when there are none (too short, no transcript, nothing usable).
    reason: Optional[str] = None


@router.post("/{audio_asset_id}/chapters", response_model=ChaptersResponse)
@limiter.limit("10/minute")
async def generate_chapters(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> ChaptersResponse:
    """Finds where the topic changes in the real transcript and saves the chapters on the
    recording. One AI call. Every time and title is checked against the recording before it
    is kept, so nothing made up is ever saved; a set with nothing usable is not saved."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    words = doc.get("transcript") or []
    if not words:
        raise HTTPException(status_code=400, detail="This recording has no transcript yet. Transcribe it first.")

    duration = max((float(w.get("end_s") or 0) for w in words), default=0.0)
    if duration < chapter_rules.MIN_AUDIO_SECONDS:
        return ChaptersResponse(chapters=[], reason="This recording is under a minute, so it is not split into chapters.")

    await assert_ai_budget_available(ctx.workspace_id)
    set_usage_workspace(ctx.workspace_id)

    language = doc.get("language") or await workspace_language(ctx.workspace_id) or "en"
    prompt = load_prompt(
        "audio/chapters",
        lines=chapter_rules.transcript_for_prompt(words),
        min_chapters=2 if duration < 300 else 3,
        max_chapters=min(chapter_rules.MAX_CHAPTERS, max(3, int(duration // 120) + 2)),
        language_name=resolve_language_directive_name(language),
    )
    try:
        result = await call_llm_structured(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chapter generation failed for %s: %s", audio_asset_id, exc)
        raise HTTPException(status_code=502, detail="Couldn't find chapters right now. Try again.")

    chapters = chapter_rules.normalize_chapters((result or {}).get("chapters"), duration)
    if len(chapters) < 2:
        return ChaptersResponse(chapters=[], reason="Couldn't find clear chapters in this recording.")

    await audio_assets.update_one(
        {"id": audio_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {"chapters": chapters, "updated_at": datetime.now(timezone.utc)}},
    )
    return ChaptersResponse(chapters=chapters)


@router.post("/{audio_asset_id}/suggest-clips", response_model=SuggestClipsResponse)
@limiter.limit("5/minute")
async def suggest_clips_for_audio_asset(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> SuggestClipsResponse:
    """Always suggestions to review, never applied by themselves."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    suggestions = await _generate_clip_suggestions(doc, ctx.workspace_id, audio_asset_id)
    return SuggestClipsResponse(suggestions=suggestions)


@router.post("/{audio_asset_id}/soundbites", response_model=list[SoundbiteOut], status_code=201)
@limiter.limit("5/minute")
async def extract_soundbites(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> list[SoundbiteOut]:
    """Real extraction, for the Batch Approval Queue: gets real moment
    suggestions from the real transcript, actually trims each one out of
    the real master audio, measures it for real quality issues, and
    stores each as a real, persisted Soundbite — not a mock list."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    if not doc.get("media_id"):
        raise HTTPException(status_code=400, detail="This recording has no audio file to extract soundbites from.")

    suggestions = await _generate_clip_suggestions(doc, ctx.workspace_id, audio_asset_id)
    if not suggestions:
        raise HTTPException(status_code=502, detail="Couldn't find any real soundbite moments in this recording.")

    media = await media_assets.find_one({"id": doc["media_id"], "workspace_id": ctx.workspace_id})
    if not media:
        raise HTTPException(status_code=404, detail="The master recording's audio file is missing.")
    master_bytes = await _download_media_bytes(media["url"])

    created: list[SoundbiteOut] = []
    now = datetime.now(timezone.utc)
    for suggestion in suggestions:
        try:
            clip_bytes = trim_span(master_bytes, suggestion.start_s, suggestion.end_s)
        except SoundbiteExtractionError as exc:
            logger.warning("Soundbite trim skipped for %s: %s", audio_asset_id, exc)
            continue
        status, confidence, flag_message, measured_lufs = evaluate_quality(clip_bytes)

        url = await upload_file(clip_bytes, UploadContentType.AUDIO, ctx.user_id)
        clip_media = MediaAsset(
            id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.AUDIO, url=url,
            mime_type="audio/wav", source=MediaSource.EDITED, created_by=ctx.user_id, created_at=now,
            size_bytes=len(clip_bytes), duration_s=round(suggestion.end_s - suggestion.start_s, 2),
        )
        await media_assets.insert_one(clip_media.model_dump())

        soundbite = Soundbite(
            id=uuid4().hex, audio_asset_id=audio_asset_id, workspace_id=ctx.workspace_id,
            media_id=clip_media.id, quote=suggestion.quote, reason=suggestion.reason,
            start_s=suggestion.start_s, end_s=suggestion.end_s,
            duration_s=round(suggestion.end_s - suggestion.start_s, 2),
            status=status, flag_message=flag_message, confidence=confidence, measured_lufs=measured_lufs,
            created_by=ctx.user_id, created_at=now,
        )
        await soundbites.insert_one(soundbite.model_dump())
        created.append(SoundbiteOut(**soundbite.model_dump(), url=url))

    if not created:
        raise HTTPException(status_code=502, detail="None of the suggested spans could be extracted. Try again.")
    return created


@router.get("/{audio_asset_id}/soundbites", response_model=list[SoundbiteOut])
async def list_soundbites(
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> list[SoundbiteOut]:
    docs = await soundbites.find(
        {"audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id},
    ).sort("created_at", 1).to_list(length=100)
    if not docs:
        return []
    media_ids = [d["media_id"] for d in docs]
    found = await media_assets.find({"id": {"$in": media_ids}}).to_list(length=len(media_ids))
    urls = {m["id"]: m["url"] for m in found}
    return [SoundbiteOut(**d, url=urls[d["media_id"]]) for d in docs if d["media_id"] in urls]


@router.patch("/soundbites/{soundbite_id}/approve", response_model=SoundbiteOut)
async def approve_soundbite(
    soundbite_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> SoundbiteOut:
    result = await soundbites.find_one_and_update(
        {"id": soundbite_id, "workspace_id": ctx.workspace_id},
        {"$set": {"approval_status": AudioApprovalStatus.APPROVED.value}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Soundbite not found.")
    media = await media_assets.find_one({"id": result["media_id"]})
    return SoundbiteOut(**result, url=media["url"] if media else "")


@router.patch("/soundbites/{soundbite_id}/reject", response_model=SoundbiteOut)
async def reject_soundbite(
    soundbite_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> SoundbiteOut:
    result = await soundbites.find_one_and_update(
        {"id": soundbite_id, "workspace_id": ctx.workspace_id},
        {"$set": {"approval_status": AudioApprovalStatus.REJECTED.value}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Soundbite not found.")
    media = await media_assets.find_one({"id": result["media_id"]})
    return SoundbiteOut(**result, url=media["url"] if media else "")


class RefineSoundbitesRequest(BaseModel):
    instruction: str


_REFINE_ACTIONS = {"prevent_clipping", "remove_filler_pauses", "recheck_quality"}


@router.post("/{audio_asset_id}/soundbites/refine", response_model=list[SoundbiteOut])
@limiter.limit("5/minute")
async def refine_soundbites(
    request: Request,
    audio_asset_id: str,
    body: RefineSoundbitesRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> list[SoundbiteOut]:
    """Real, bounded natural-language refinement: one gated LLM call maps
    free text onto a fixed set of 3 real actions (never an arbitrary one),
    then actually applies them to every pending soundbite via the same
    real DSP `apply_cleanup` the Cleanup tab uses."""
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Type what you want changed first.")

    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")

    targets = await soundbites.find(
        {"audio_asset_id": audio_asset_id, "workspace_id": ctx.workspace_id, "approval_status": "pending"},
    ).to_list(length=100)
    if not targets:
        raise HTTPException(status_code=400, detail="No pending soundbites to refine.")

    await assert_ai_budget_available(ctx.workspace_id)
    set_usage_workspace(ctx.workspace_id)
    prompt = load_prompt("audio/soundbite_refine", instruction=instruction)
    try:
        result = await call_llm_structured(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Soundbite refine mapping failed for %s: %s", audio_asset_id, exc)
        raise HTTPException(status_code=502, detail="Couldn't understand that instruction right now. Try again.")

    actions = [a for a in (result.get("actions") or []) if a in _REFINE_ACTIONS]
    if not actions:
        raise HTTPException(status_code=400, detail="That instruction doesn't match anything real this can do yet.")

    parent_words = doc.get("transcript", [])
    updated: list[SoundbiteOut] = []
    for target in targets:
        media = await media_assets.find_one({"id": target["media_id"], "workspace_id": ctx.workspace_id})
        if not media:
            continue
        clip_bytes = await _download_media_bytes(media["url"])

        settings_kwargs: dict = {}
        if "prevent_clipping" in actions:
            settings_kwargs["compressor"] = 0.6
        clip_words = None
        if "remove_filler_pauses" in actions:
            clip_words = [
                {**w, "start_s": w["start_s"] - target["start_s"], "end_s": w["end_s"] - target["start_s"]}
                for w in parent_words
                if target["start_s"] <= w["start_s"] < target["end_s"]
            ]
            if clip_words:  # apply_cleanup rejects remove_fillers with no transcript
                settings_kwargs["remove_fillers"] = True
                settings_kwargs["silence_trim_s"] = 0.75

        cleaned = clip_bytes
        if settings_kwargs:
            try:
                cleaned, _, _ = await asyncio.to_thread(
                    apply_cleanup, clip_bytes, CleanupSettings(**settings_kwargs), clip_words or [],
                )
            except CleanupError as exc:
                logger.warning("Soundbite refine cleanup skipped for %s: %s", target["id"], exc)

        status, confidence, flag_message, measured_lufs = evaluate_quality(cleaned)

        if cleaned != clip_bytes:
            new_url = await upload_file(cleaned, UploadContentType.AUDIO, ctx.user_id)
            now = datetime.now(timezone.utc)
            new_media = MediaAsset(
                id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.AUDIO, url=new_url,
                mime_type="audio/wav", source=MediaSource.ENHANCED, created_by=ctx.user_id,
                created_at=now, size_bytes=len(cleaned),
            )
            await media_assets.insert_one(new_media.model_dump())
            media_id, url = new_media.id, new_url
        else:
            media_id, url = target["media_id"], media["url"]

        await soundbites.update_one(
            {"id": target["id"]},
            {"$set": {
                "media_id": media_id, "status": status.value, "confidence": confidence,
                "flag_message": flag_message, "measured_lufs": measured_lufs,
            }},
        )
        refreshed = await soundbites.find_one({"id": target["id"]})
        updated.append(SoundbiteOut(**refreshed, url=url))

    return updated


@router.get("/{audio_asset_id}", response_model=AudioAsset)
@limiter.limit("120/minute")
async def get_audio_asset(
    request: Request,
    audio_asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> AudioAsset:
    """One audio project with everything the Audio pipeline needs to open it again (script, transcript, versions in use,
    video clips). Used by "Open in Audio pipeline" and the History tab. Defined last so it never shadows a fixed path."""
    doc = await audio_assets.find_one({"id": audio_asset_id, "workspace_id": ctx.workspace_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Audio asset not found.")
    return AudioAsset(**doc)
