"""The transcript of a library video: start it, see how far it is, read it in pages, search it, and get caption files.

The transcript is made once per video, in pieces (see app.video.transcribe), and survives restarts and the free speech allowance
running out: a long file shows what it will cost first and the rest is queued. Every response is sent fresh.
"""
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import media_assets
from app.models.media import MediaTranscriptWord
from app.pipelines.media.captions import build_cues, to_srt, to_vtt
from app.shared import pipeline_runs
from app.video import transcribe

logger = logging.getLogger(__name__)


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])


async def _media(ctx: WorkspaceContext, media_id: str) -> dict:
    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    kind = (media or {}).get("kind")
    kind = kind.value if hasattr(kind, "value") else kind
    if not media or kind not in ("video", "audio"):
        raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
    return media


def _duration(media: dict) -> float:
    probe = media.get("probe") or {}
    return float(probe.get("duration_s") or media.get("duration_s") or 0)


async def _estimate(media: dict, existing: Optional[dict]) -> dict[str, Any]:
    seconds = _duration(media)
    chunks = transcribe.plan_chunks(seconds)
    left = await transcribe.budget_left()
    todo = sum(c["length_s"] for c in chunks) if not existing else sum(c["length_s"] for c in existing["chunks"] if c["status"] != "done")
    fits_now = todo <= min(left["hour_left_s"], left["day_left_s"])
    waits = not fits_now
    return {
        "duration_s": seconds, "pieces": len(chunks), "speech_seconds_to_read": round(todo, 1),
        "hour_left_s": round(left["hour_left_s"]), "day_left_s": round(left["day_left_s"]),
        "finishes_now": fits_now,
        "note": None if fits_now else (
            "This is more than the free speech allowance has left this hour. It starts now and carries on by itself as the "
            "allowance comes back; you can leave the page."
        ),
        "waits": waits,
    }


@router.get("/{media_id}/estimate")
@limiter.limit("60/minute")
async def estimate(request: Request, media_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    media = await _media(ctx, media_id)
    if _duration(media) <= 0:
        raise HTTPException(status_code=400, detail="This video's length is not known yet.")
    existing = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    return await _estimate(media, existing)


class StartBody(BaseModel):
    language: Optional[str] = Field(None, max_length=12)
    speakers: bool = False


@router.post("/{media_id}", status_code=202)
@limiter.limit("20/minute")
async def start(
    request: Request, media_id: str, body: StartBody = StartBody(), ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict[str, Any]:
    from app.video import governance

    if not await governance.video_enabled(ctx.workspace_id):
        raise HTTPException(status_code=403, detail=governance.ACCESS_OFF)
    if not await governance.transcription_allowed(ctx.workspace_id):
        raise HTTPException(status_code=409, detail=governance.PRIVACY_OFF)
    media = await _media(ctx, media_id)
    duration = _duration(media)
    if duration <= 0:
        raise HTTPException(status_code=400, detail="This video's length is not known yet, so it can't be transcribed.")
    doc = await transcribe.create(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, media_id=media_id, duration_s=duration,
        language=body.language, speakers=body.speakers,
    )
    if doc["status"] in ("done", "partial"):
        return {"transcript": transcribe.public(doc), "run": None}
    run = None
    try:
        await pipeline_runs.assert_capacity(ctx.workspace_id)
        run = await pipeline_runs.create_run(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind="video", title="Transcribing a video",
            steps_total=max(1, len(doc["chunks"])), ref={"media_id": media_id, "transcript_id": doc["id"]},
            href=f"/dashboard/pipelines/video?media={media_id}", active_key=f"video-transcript:{doc['id']}",
        )
    except pipeline_runs.RunAlreadyActive as exc:
        return {"transcript": transcribe.public(doc), "run": pipeline_runs.public(exc.existing)}

    transcript_id = doc["id"]

    async def work(row: dict) -> dict:
        reporter = pipeline_runs.StepReporter(row["id"])
        state = await transcribe.run(transcript_id, reporter=reporter)
        return {"data": state, "href": f"/dashboard/pipelines/video?media={media_id}"}

    pipeline_runs.start(run, work, interrupt_on_cancel=True)
    return {"transcript": transcribe.public(doc), "run": pipeline_runs.public(run)}


@router.get("/{media_id}")
@limiter.limit("240/minute")
async def get_transcript(request: Request, media_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    await _media(ctx, media_id)
    doc = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not doc:
        return {"transcript": None}
    return {"transcript": transcribe.public(doc)}


@router.get("/{media_id}/words")
@limiter.limit("240/minute")
async def words(
    request: Request, media_id: str, page: int = Query(0, ge=0), ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    await _media(ctx, media_id)
    doc = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not doc or doc["status"] not in ("done", "partial"):
        raise HTTPException(status_code=409, detail="This video has no finished transcript yet.")
    pages = max(1, -(-int(doc.get("word_count") or 0) // transcribe.PAGE_WORDS))
    return {"page": page, "pages": pages, "words": await transcribe.words_page(doc["id"], page), "language": doc.get("language")}


@router.get("/{media_id}/search")
@limiter.limit("120/minute")
async def search(
    request: Request, media_id: str, q: str = Query(..., min_length=1, max_length=200), ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    await _media(ctx, media_id)
    doc = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not doc or doc["status"] not in ("done", "partial"):
        raise HTTPException(status_code=409, detail="This video has no finished transcript yet.")
    return {"query": q, "hits": transcribe.search(await transcribe.all_words(doc["id"]), q)}


@router.get("/{media_id}/captions")
@limiter.limit("60/minute")
async def captions(
    request: Request, media_id: str, format: Literal["srt", "vtt"] = "srt", ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Response:
    await _media(ctx, media_id)
    doc = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not doc or doc["status"] not in ("done", "partial") or not doc.get("word_count"):
        raise HTTPException(status_code=409, detail="This video has no transcript to make captions from yet.")
    rows = await transcribe.all_words(doc["id"])
    cues = build_cues([MediaTranscriptWord(word=w["w"], start_s=w["s_ms"] / 1000.0, end_s=w["e_ms"] / 1000.0) for w in rows])
    body = to_srt(cues) if format == "srt" else to_vtt(cues)
    media_type = "application/x-subrip" if format == "srt" else "text/vtt"
    return Response(content=body, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="captions.{format}"'})


@router.post("/{media_id}/retry", status_code=202)
@limiter.limit("20/minute")
async def retry(request: Request, media_id: str, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    await _media(ctx, media_id)
    doc = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not doc:
        raise HTTPException(status_code=404, detail="This video has not been transcribed yet.")
    await transcribe.retry_failed(doc["id"])
    return {"transcript": transcribe.public(await transcribe._transcripts().find_one({"id": doc["id"]}))}
