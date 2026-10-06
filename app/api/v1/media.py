"""Generic media upload — the real MediaAsset contract every content-creation
surface shares (text pipeline's default-image picker, manual attach, future
audio/video pipelines). Same Cloudinary-through-backend pattern campaigns'
thumbnail upload already used (app.shared.storage), extended to video/audio.

width/height/duration_s come straight from Cloudinary's own upload response
(it measures every file it ingests) — they used to be dropped, leaving these
fields permanently unset. POST /{id}/analyze then adds the recording's
transcript and chapters for video/audio, and GET /{id}/captions serves them
as SRT/VTT.
"""

import logging
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from app.agents.supervisor.service import assert_ai_budget_available
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import media_assets, workspaces
from app.models.agent_events import ContentEventPayload, ContentRef, EventType
from app.models.media import MediaAnalysisStatus, MediaAsset, MediaKind, MediaSource, MediaTranscriptWord
from app.models.workspace import MediaUploadLimits
from app.pipelines.media.captions import build_cues, to_srt, to_vtt
from app.pipelines.media.transform import ASPECT_RATIO_PRESETS, build_transformed_url
from app.pipelines.media.video_analysis import analyze_media, poster_url_for
from app.shared.events import emit_event_background
from app.shared.llm import set_usage_workspace
from app.shared.pipeline_types import PipelineType
from app.shared.storage import ContentType as UploadContentType, upload_file_detailed

logger = logging.getLogger(__name__)
router = APIRouter()

# Fallback when a workspace has never set its own limits (or the lookup
# itself fails) — the original hardcoded values, unchanged. Real limits
# now live on Workspace.media_upload_limits (Plan & Quotas), configurable
# per workspace instead of one fixed constant for everyone — the intended
# hook for varying this by pricing tier later.
_DEFAULT_LIMITS = MediaUploadLimits()

_LIMIT_FIELD: dict[MediaKind, str] = {
    MediaKind.IMAGE: "image_mb",
    MediaKind.VIDEO: "video_mb",
    MediaKind.AUDIO: "audio_mb",
}


async def _max_bytes_for(kind: MediaKind, workspace_id: str) -> int:
    limits = _DEFAULT_LIMITS
    try:
        ws = await workspaces.find_one({"id": workspace_id}, {"media_upload_limits": 1})
        if ws and ws.get("media_upload_limits"):
            limits = MediaUploadLimits(**ws["media_upload_limits"])
    except Exception as exc:  # noqa: BLE001 — never block an upload over a settings-read failure
        logger.warning("Failed to read media_upload_limits for workspace %s, using defaults: %s", workspace_id, exc)
    return getattr(limits, _LIMIT_FIELD[kind]) * 1024 * 1024

ALLOWED_MIME_TYPES: dict[MediaKind, set[str]] = {
    MediaKind.IMAGE: {"image/jpeg", "image/png", "image/webp"},
    # AVI has no single standardized browser-reported mime type — accepting
    # the real variants different browsers/OSes actually send (verified
    # live, not guessed) rather than silently rejecting a valid upload
    # because of which one showed up. All 5 formats here are confirmed
    # Cloudinary video formats (MKV is not, deliberately left out).
    MediaKind.VIDEO: {
        "video/mp4", "video/quicktime", "video/webm",
        "video/x-msvideo", "video/avi", "video/msvideo",
        "video/x-flv",
    },
    MediaKind.AUDIO: {"audio/mpeg", "audio/wav", "audio/mp4", "audio/webm"},
}

_MIME_TO_UPLOAD_TYPE: dict[MediaKind, UploadContentType] = {
    MediaKind.IMAGE: UploadContentType.IMAGE,
    MediaKind.VIDEO: UploadContentType.VIDEO,
    MediaKind.AUDIO: UploadContentType.AUDIO,
}


def _resolve_kind(content_type: str) -> MediaKind | None:
    for kind, allowed in ALLOWED_MIME_TYPES.items():
        if content_type in allowed:
            return kind
    return None


@router.post("", response_model=MediaAsset, status_code=201)
@limiter.limit("20/minute")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> MediaAsset:
    """Upload an image/video/audio file, returning a real MediaAsset to
    attach to a piece (GeneratedPiece.media) or reference directly in a
    publish request — never re-uploaded per platform after this."""
    kind = _resolve_kind(file.content_type or "")
    if kind is None:
        allowed = sorted({m for s in ALLOWED_MIME_TYPES.values() for m in s})
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported media type '{file.content_type}'. Allowed: {', '.join(allowed)}.",
        )

    contents = await file.read()
    max_bytes = await _max_bytes_for(kind, ctx.workspace_id)
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"{kind.value.capitalize()} must be {max_bytes // (1024 * 1024)}MB or smaller.",
        )

    if kind == MediaKind.IMAGE:
        from app.agents.content_guard.media import assert_image_ok

        await assert_image_ok(contents, file.content_type or "image/jpeg", workspace_id=ctx.workspace_id, where="upload")
    spoken_words = None
    if kind in (MediaKind.AUDIO, MediaKind.VIDEO):
        from app.agents.content_guard.media import read_upload_speech

        spoken_words = await read_upload_speech(
            contents, file.filename or "upload", noun="video" if kind == MediaKind.VIDEO else "recording",
            workspace_id=ctx.workspace_id,
        )

    try:
        uploaded = await upload_file_detailed(contents, _MIME_TO_UPLOAD_TYPE[kind], ctx.user_id)
    except Exception as exc:  # noqa: BLE001 — same tolerance as campaigns' thumbnail upload
        logger.error("Media upload failed for workspace %s: %s", ctx.workspace_id, exc)
        raise HTTPException(status_code=502, detail="Media upload failed. Try again.")

    asset = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=kind,
        url=uploaded["url"],
        mime_type=file.content_type,
        size_bytes=len(contents),
        width=uploaded.get("width") if kind != MediaKind.AUDIO else None,
        height=uploaded.get("height") if kind != MediaKind.AUDIO else None,
        duration_s=uploaded.get("duration_s") if kind != MediaKind.IMAGE else None,
        poster_url=poster_url_for(uploaded["url"]) if kind == MediaKind.VIDEO else None,
        source=MediaSource.UPLOADED,
        created_by=ctx.user_id,
        created_at=datetime.now(timezone.utc),
    )
    document = asset.model_dump()
    if spoken_words:
        document["transcript"] = spoken_words  # already read for the content check, so publishing does not read it again
    await media_assets.insert_one(document)
    return asset


async def _get_media_or_404(media_id: str, workspace_id: str) -> dict:
    doc = await media_assets.find_one({"id": media_id, "workspace_id": workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Media not found.")
    return doc


@router.post("/{media_id}/analyze", response_model=MediaAsset)
@limiter.limit("10/minute")
async def analyze_media_asset(
    request: Request,
    media_id: str,
    force: bool = Query(False, description="Re-run even if this recording was already analysed."),
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> MediaAsset:
    """Transcribe a video/audio recording and build its real chapters.

    Idempotent: an already-analysed recording is returned as-is unless
    `force`. A recording that can't be transcribed still returns 200 with
    analysis_status="failed" and a plain reason, so the caller can show it
    instead of treating it as a server error."""
    doc = await _get_media_or_404(media_id, ctx.workspace_id)
    if doc.get("kind") not in (MediaKind.VIDEO.value, MediaKind.AUDIO.value):
        raise HTTPException(status_code=400, detail="Only video and audio recordings can be analysed.")

    if doc.get("analysis_status") == MediaAnalysisStatus.DONE.value and not force:
        return MediaAsset(**doc)

    # Chapter naming is an LLM call: count it toward, and respect, the budget.
    await assert_ai_budget_available(ctx.workspace_id)
    set_usage_workspace(ctx.workspace_id)

    update = await analyze_media(doc)
    await media_assets.update_one({"id": media_id, "workspace_id": ctx.workspace_id}, {"$set": update})

    # A video only becomes something Remy and Odette can read once it has been
    # analysed, so this is the moment it "exists" for them. Announced once —
    # a forced re-run of an already-analysed recording must not double-count it.
    first_time = doc.get("analysis_status") != MediaAnalysisStatus.DONE.value
    if doc.get("kind") == MediaKind.VIDEO.value and first_time and update.get("analysis_status") == MediaAnalysisStatus.DONE.value:
        spoken = " ".join(w["word"].strip() for w in update.get("transcript", []) if w["word"].strip())
        emit_event_background(
            event_type=EventType.CONTENT_CREATED,
            pipeline_type=PipelineType.VIDEO,
            workspace_id=ctx.workspace_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            payload=ContentEventPayload(
                content_id=media_id,
                content_ref=ContentRef(collection="media_assets", id=media_id),
                content_text=spoken,
                content_summary=spoken[:400],
            ),
        )
    return MediaAsset(**{**doc, **update})


@router.get("/{media_id}/captions")
@limiter.limit("60/minute")
async def download_captions(
    request: Request,
    media_id: str,
    format: Literal["srt", "vtt"] = Query("srt"),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Response:
    """Subtitle file (SRT or WebVTT) built from the recording's real,
    word-timed transcript."""
    doc = await _get_media_or_404(media_id, ctx.workspace_id)
    words = [MediaTranscriptWord(**w) for w in doc.get("transcript") or []]
    if not words:
        raise HTTPException(
            status_code=404,
            detail="No transcript yet. Analyse this recording first to create captions.",
        )
    cues = build_cues(words)
    body = to_srt(cues) if format == "srt" else to_vtt(cues)
    return Response(
        content=body,
        media_type="text/vtt" if format == "vtt" else "application/x-subrip",
        headers={"Content-Disposition": f'attachment; filename="captions-{media_id[:8]}.{format}"'},
    )


class TransformMediaRequest(BaseModel):
    # "original"/None/omitted = no crop requested. See ASPECT_RATIO_PRESETS
    # for the real, supported set — not free-form width/height, which would
    # need real per-asset dimension math this endpoint doesn't do.
    aspect_ratio: Optional[str] = None
    trim_start_s: Optional[float] = None
    trim_end_s: Optional[float] = None


# Which quick actions make sense per kind — cropping audio to an aspect
# ratio is meaningless (no visual dimension), trimming an image is
# meaningless (no time dimension). Enforced here, not just left to the
# frontend to get right.
_ASPECT_RATIO_ALLOWED_KINDS = {MediaKind.IMAGE, MediaKind.VIDEO}
_TRIM_ALLOWED_KINDS = {MediaKind.VIDEO, MediaKind.AUDIO}


@router.post("/{media_id}/transform", response_model=MediaAsset, status_code=201)
@limiter.limit("30/minute")
async def transform_media(
    request: Request,
    media_id: str,
    body: TransformMediaRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> MediaAsset:
    """Quick-action edit (trim / crop-to-aspect-ratio) via Cloudinary's own
    URL transformation parameters — no ffmpeg, no new infra. Creates a
    real, distinct new MediaAsset (source=EDITED) rather than mutating the
    original, so the source stays intact and both remain independently
    referenceable (e.g. if a piece already references the original)."""
    doc = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Media not found.")
    source = MediaAsset(**doc)

    want_crop = bool(body.aspect_ratio and body.aspect_ratio != "original")
    want_trim = body.trim_start_s is not None or body.trim_end_s is not None

    if not want_crop and not want_trim:
        raise HTTPException(status_code=400, detail="Nothing to change — pick a crop or trim.")
    if want_crop and source.kind not in _ASPECT_RATIO_ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"Can't crop {source.kind.value} to an aspect ratio.")
    if want_trim and source.kind not in _TRIM_ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"Can't trim {source.kind.value}.")
    if want_crop and body.aspect_ratio not in ASPECT_RATIO_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported aspect ratio. Choose one of: {', '.join(ASPECT_RATIO_PRESETS)}.",
        )
    # Neither build_transformed_url nor anything above validates the trim
    # values themselves — a negative or inverted range (trim_end_s before
    # trim_start_s) passed every existing check and reached the URL builder
    # unvalidated, which then computed a negative duration_s below and
    # stored it. The shipped UI self-clamps and can't produce this, but the
    # endpoint itself must reject it for any other caller.
    if want_trim:
        if body.trim_start_s is not None and body.trim_start_s < 0:
            raise HTTPException(status_code=400, detail="trim_start_s can't be negative.")
        if body.trim_end_s is not None and body.trim_end_s < 0:
            raise HTTPException(status_code=400, detail="trim_end_s can't be negative.")
        if (
            body.trim_start_s is not None
            and body.trim_end_s is not None
            and body.trim_end_s <= body.trim_start_s
        ):
            raise HTTPException(status_code=400, detail="trim_end_s must be after trim_start_s.")

    try:
        new_url = build_transformed_url(
            source.url,
            kind=source.kind,
            aspect_ratio=body.aspect_ratio if want_crop else None,
            trim_start_s=body.trim_start_s,
            trim_end_s=body.trim_end_s,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    asset = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=source.kind,
        url=new_url,
        mime_type=source.mime_type,
        # Cropping changes real dimensions; trimming doesn't. Only known
        # for a square/16:9/9:16/4:5 crop if the source's own width/height
        # were known — left unset otherwise rather than guessed, same
        # honest gap as upload's own width/height-unset note above.
        width=None,
        height=None,
        duration_s=(body.trim_end_s - body.trim_start_s) if want_trim and body.trim_start_s is not None and body.trim_end_s is not None else None,
        source=MediaSource.EDITED,
        created_by=ctx.user_id,
        created_at=datetime.now(timezone.utc),
    )
    await media_assets.insert_one(asset.model_dump())
    return asset


@router.get("/{media_id}", response_model=MediaAsset)
@limiter.limit("60/minute")
async def get_media(
    request: Request, media_id: str, ctx: WorkspaceContext = Depends(require("create_content"))
) -> MediaAsset:
    doc = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Media not found.")
    return MediaAsset(**doc)
