"""Voices for a video: dub it into another language, narrate a video that has no speech, several voices in one scene, caption
translations for a whole library, and the list of voice tracks a video has. Every long piece of work is a run in the activity
dock; every spoken line is placed from the real length of its audio and flagged when it cannot fit."""
import io
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db, media_assets
from app.models.text import LANGUAGE_NAMES
from app.shared import pipeline_runs
from app.video import service, transcribe
from app.video.create import voice as voice_mod
from app.video.narrate import dub as D
from app.video.narrate import plan as P
from app.video.narrate import service as N

logger = logging.getLogger(__name__)


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])


def _dubs():
    return get_db()["video_dubs"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Optional[datetime]) -> Optional[str]:
    return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z") if v else None


def resolve_language(value: str) -> tuple[str, str]:
    """(code, plain name) for "ta", "Tamil" or "ta-IN". The name is what the translation prompt and the voice check use."""
    raw = (value or "").strip()
    low = raw.lower()
    base = low.split("-")[0]
    if base in LANGUAGE_NAMES:
        return base, LANGUAGE_NAMES[base]
    for code, name in LANGUAGE_NAMES.items():
        if name.lower() == low:
            return code, name
    if not raw or "+" in raw or len(raw) > 30:
        raise HTTPException(status_code=400, detail="Choose one language to dub into.")
    return base, raw.title()


async def _video(ctx: WorkspaceContext, media_id: str, *, need_speech: bool = False) -> dict:
    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    kind = (media or {}).get("kind")
    kind = kind.value if hasattr(kind, "value") else kind
    if not media or kind != "video":
        raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
    return media


async def _words(ctx: WorkspaceContext, media_id: str) -> tuple[dict, list[dict]]:
    tr = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not tr or tr["status"] not in ("done", "partial"):
        raise HTTPException(status_code=409, detail="Write out what was said in this video first. A dub is made from the transcript.")
    return tr, await transcribe.all_words(tr["id"])


async def _voice_tools(ctx: WorkspaceContext):
    """(speak, store, forget, voice_settings) built from the member's own saved voice and lexicon."""
    from app.api.v1.audio_assets import _get_lexicon, _get_voice_settings
    from app.pipelines.media.tts_generation import synthesize_speech
    from app.shared.storage import ContentType as UploadContentType, delete_file_by_url, upload_file

    settings_obj = await _get_voice_settings(ctx.workspace_id, ctx.user_id)
    lexicon = await _get_lexicon(ctx.workspace_id, ctx.user_id)

    def speaker_settings(speaker: Optional[str], voices: dict[str, str]):
        if speaker and voices.get(speaker):
            return settings_obj.model_copy(update={"tts_voice": voices[speaker]})
        return settings_obj

    def make_speak(language_code: Optional[str], voices: Optional[dict[str, str]] = None):
        from app.video.narrate.voices import Voice

        async def draft_or_premium(text: str, speaker: Optional[str] = None) -> Optional[bytes]:
            return await synthesize_speech(
                text=text, voice_settings=speaker_settings(speaker, voices or {}), lexicon=lexicon, workspace_id=ctx.workspace_id,
                user_id=ctx.user_id, language=language_code,
            )

        return Voice(workspace_id=ctx.workspace_id, user_id=ctx.user_id, language=language_code, fallback=draft_or_premium, voice_settings=settings_obj,
                     voices=voices, forget=delete_file_by_url)

    async def store(data: bytes) -> str:
        return await upload_file(data, UploadContentType.AUDIO, ctx.user_id)

    return make_speak, store, delete_file_by_url, settings_obj


class DubBody(BaseModel):
    media_id: str
    language: str = Field(..., min_length=2, max_length=30)
    register_note: str = Field("", max_length=300)
    glossary: list[str] = Field(default_factory=list, max_length=60)
    tighten: bool = True
    captions: bool = True
    keep_original_db: float = Field(-18.0, ge=-40, le=0)


@router.post("/dub", status_code=202)
@limiter.limit("5/minute")
async def start_dub(request: Request, body: DubBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    from app.agents.supervisor.service import assert_ai_budget_available
    from app.pipelines.media.tts_generation import narration_blocker

    media = await _video(ctx, body.media_id)
    tr, words = await _words(ctx, body.media_id)
    code, name = resolve_language(body.language)
    if code == (tr.get("language") or "").split("-")[0].lower():
        raise HTTPException(status_code=400, detail=f"This video is already spoken in {name}. Choose another language to dub into.")
    make_speak, store, forget, voice_settings = await _voice_tools(ctx)
    blocker = narration_blocker(name, voice_settings)
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)
    await assert_ai_budget_available(ctx.workspace_id)

    doc = {
        "id": str(uuid4()), "workspace_id": ctx.workspace_id, "user_id": ctx.user_id, "source_media_id": body.media_id, "language": code,
        "language_name": name, "status": "running", "media_id": None, "job_id": None, "lines": [], "summary": None, "translate": None,
        "error": None, "created_at": _now(), "updated_at": _now(),
    }
    running = await _dubs().find_one({"workspace_id": ctx.workspace_id, "source_media_id": body.media_id, "language": code, "status": "running"})
    if running:
        return {"dub": _public(running), "run": None}
    try:
        await pipeline_runs.assert_capacity(ctx.workspace_id)
        run = await pipeline_runs.create_run(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind="video", title=f"Dubbing into {name}", steps_total=60,
            ref={"media_id": body.media_id, "dub_id": doc["id"]}, href=f"/dashboard/pipelines/video?media={body.media_id}",
            active_key=f"video-dub:{body.media_id}:{code}",
        )
    except pipeline_runs.RunAlreadyActive as exc:
        return {"dub": _public(doc), "run": pipeline_runs.public(exc.existing)}
    await _dubs().insert_one(dict(doc))
    voice = await voice_mod.load(ctx.workspace_id, None)
    note = body.register_note or ""

    async def work(row: dict) -> dict:
        reporter = pipeline_runs.StepReporter(row["id"])
        try:
            result = await N.make_dub(
                workspace_id=ctx.workspace_id, user_id=ctx.user_id, media=media, words=words, target_code=code, target_name=name,
                speak=make_speak(code), store=store, forget=forget, register_note=note, glossary=body.glossary, reporter=reporter,
                keep_original_db=body.keep_original_db, tighten=body.tighten, captions=body.captions,
            )
        except service.RenderFailed as exc:
            await _dubs().update_one({"id": doc["id"]}, {"$set": {"status": "failed", "error": str(exc), "updated_at": _now()}})
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dub %s failed: %s", doc["id"], exc)
            await _dubs().update_one({"id": doc["id"]}, {"$set": {
                "status": "failed", "error": "The dub could not be made right now. Please try again.", "updated_at": _now()}})
            raise
        await _dubs().update_one({"id": doc["id"]}, {"$set": {
            "status": "done", "media_id": result["media_id"], "job_id": result["job_id"], "lines": result["lines"], "summary": result["summary"],
            "translate": result["translate"], "voice_version": voice.get("version"), "voice_used": result.get("voice"), "updated_at": _now()}})
        return {"data": {"media_id": result["media_id"], "dub_id": doc["id"]}, "href": f"/dashboard/pipelines/video?media={result['media_id']}"}

    pipeline_runs.start(run, work, interrupt_on_cancel=True)
    return {"dub": _public(doc), "run": pipeline_runs.public(run)}


def _public(d: dict) -> dict:
    return {
        "id": d["id"], "source_media_id": d["source_media_id"], "language": d["language"], "language_name": d.get("language_name"),
        "status": d["status"], "media_id": d.get("media_id"), "summary": d.get("summary"), "lines": d.get("lines") or [],
        "error": d.get("error"), "translate": d.get("translate"), "voice_used": d.get("voice_used"), "created_at": _iso(d.get("created_at")), "updated_at": _iso(d.get("updated_at")),
    }


@router.get("/dub")
@limiter.limit("120/minute")
async def list_dubs(request: Request, media_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    await _video(ctx, media_id)
    rows = await _dubs().find({"workspace_id": ctx.workspace_id, "source_media_id": media_id}).sort("created_at", -1).limit(50).to_list(50)
    return {"dubs": [_public(r) for r in rows]}


@router.get("/dub/{dub_id}")
@limiter.limit("240/minute")
async def get_dub(request: Request, dub_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    row = await _dubs().find_one({"id": dub_id, "workspace_id": ctx.workspace_id})
    if not row:
        raise HTTPException(status_code=404, detail="That dub was not found.")
    return _public(row)


@router.get("/dub/{dub_id}/check")
@limiter.limit("60/minute")
async def check_dub(request: Request, dub_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """The round-trip check: the dubbed video is transcribed like any other video, and what was heard is compared with the script
    the voice was given. Until the dubbed video has a finished transcript this says so and what to do."""
    row = await _dubs().find_one({"id": dub_id, "workspace_id": ctx.workspace_id})
    if not row or row.get("status") != "done" or not row.get("media_id"):
        raise HTTPException(status_code=404, detail="That dub is not finished.")
    tr = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": row["media_id"]})
    if not tr or tr["status"] not in ("done", "partial"):
        return {"status": "needs_transcript", "media_id": row["media_id"],
                "note": "Write out what is said in the dubbed video first, then this compares it with the script."}
    heard = " ".join(w["w"] for w in await transcribe.all_words(tr["id"]))
    script = " ".join(ln["text"] for ln in row.get("lines") or [] if ln.get("text"))
    score = D.roundtrip_similarity(script, heard)
    return {"status": "done", "similarity": score, "verdict": "good" if score >= 0.8 else "check", "media_id": row["media_id"],
            "note": None if score >= 0.8 else "Some lines were not heard as written. Listen to the lines flagged for review."}


@router.get("/tracks/{media_id}")
@limiter.limit("240/minute")
async def tracks(request: Request, media_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """The voice tracks of a video: the original and every finished dub, so a track can be chosen or swapped."""
    media = await _video(ctx, media_id)
    tr = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id}, {"language": 1})
    rows = await _dubs().find({"workspace_id": ctx.workspace_id, "source_media_id": media_id, "status": "done"}).sort("created_at", -1).to_list(50)
    seen: set[str] = set()
    out = [{"id": "original", "label": "Original voice", "language": (tr or {}).get("language"), "media_id": media_id, "ai_voice": False}]
    for r in rows:
        if r["language"] in seen:
            continue
        seen.add(r["language"])
        flags = (r.get("summary") or {}).get("needs_review") or []
        out.append({"id": r["id"], "label": f"{r.get('language_name') or r['language']} dub", "language": r["language"],
                    "media_id": r.get("media_id"), "ai_voice": True, "needs_review": len(flags)})
    return {"tracks": out}


class NarrationLine(BaseModel):
    speaker: str = Field("", max_length=40)
    text: str = Field(..., min_length=1, max_length=3000)


class NarrationBody(BaseModel):
    media_id: str
    script: str = Field("", max_length=6000)
    lines: list[NarrationLine] = Field(default_factory=list, max_length=60)
    voices: dict[str, str] = Field(default_factory=dict)
    language: str = Field("en", max_length=30)
    fit_to_video: bool = False
    original_audio_db: Optional[float] = Field(None, ge=-60, le=0)
    music_media_id: Optional[str] = None
    captions: bool = True


@router.post("/narration", status_code=202)
@limiter.limit("5/minute")
async def start_narration(request: Request, body: NarrationBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    from app.agents.supervisor.service import assert_ai_budget_available
    from app.pipelines.media import duration as dur
    from app.pipelines.media import fit_script as fitter
    from app.pipelines.media.tts_generation import narration_blocker
    from app.pipelines.text.generator import resolve_language_directive_name
    from app.shared.llm import call_llm, member_footage, set_usage_workspace

    media = await _video(ctx, body.media_id)
    if not body.script.strip() and not body.lines:
        raise HTTPException(status_code=400, detail="Write the script to narrate, or add dialogue lines.")
    music_url = None
    if body.music_media_id:
        m = await media_assets.find_one({"id": body.music_media_id, "workspace_id": ctx.workspace_id})
        mk = (m or {}).get("kind")
        mk = mk.value if hasattr(mk, "value") else mk
        if not m or mk != "audio":
            raise HTTPException(status_code=404, detail="That music is not in this workspace's library.")
        music_url = m["url"]
    make_speak, store, forget, voice_settings = await _voice_tools(ctx)
    code, name = resolve_language(body.language) if body.language else ("en", "English")
    blocker = narration_blocker(name, voice_settings)
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)
    script = body.script.strip()
    fit_note: Optional[str] = None
    if body.fit_to_video and script and not body.lines:
        await assert_ai_budget_available(ctx.workspace_id)
        set_usage_workspace(ctx.workspace_id)
        video_s = float(media.get("duration_s") or 0)
        if video_s > 0:
            async def ask(prompt: str) -> str:
                async with member_footage():
                    return await call_llm(prompt, temperature=0.5, max_tokens=4000)

            fitted = await fitter.fit_script(script, video_s, None, resolve_language_directive_name(code), ask)
            script = fitted["script"]
            fit_note = fitted.get("reason") or None
    try:
        await pipeline_runs.assert_capacity(ctx.workspace_id)
        run = await pipeline_runs.create_run(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind="video", title="Narrating a video", steps_total=30,
            ref={"media_id": body.media_id}, href=f"/dashboard/pipelines/video?media={body.media_id}", active_key=f"video-narrate:{body.media_id}",
        )
    except pipeline_runs.RunAlreadyActive as exc:
        return {"run": pipeline_runs.public(exc.existing), "note": None}
    lines = [{"speaker": ln.speaker, "text": ln.text} for ln in body.lines]

    async def work(row: dict) -> dict:
        reporter = pipeline_runs.StepReporter(row["id"])
        result = await N.make_narration(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, media=media, script=script, lines=lines or None,
            speak=make_speak(code, body.voices), store=store, forget=forget, language=code, fit_to_video=body.fit_to_video,
            original_audio_db=body.original_audio_db, music_url=music_url, captions=body.captions, reporter=reporter,
        )
        return {"data": {"media_id": result["media_id"]}, "href": f"/dashboard/pipelines/video?media={result['media_id']}"}

    pipeline_runs.start(run, work, interrupt_on_cancel=True)
    return {"run": pipeline_runs.public(run), "note": fit_note}


class CaptionBatchBody(BaseModel):
    media_ids: list[str] = Field(default_factory=list, max_length=50)
    all_videos: bool = False
    language: str = Field(..., min_length=2, max_length=30)
    register_note: str = Field("", max_length=300)
    glossary: list[str] = Field(default_factory=list, max_length=60)


@router.post("/captions/localize", status_code=202)
@limiter.limit("3/minute")
async def localize_captions(request: Request, body: CaptionBatchBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Caption translations for many videos at once, one run. Each video's existing transcript is translated line by line with its
    timing kept; videos with no transcript are skipped and listed."""
    from app.agents.supervisor.service import assert_ai_budget_available

    code, name = resolve_language(body.language)
    targets: list[dict] = []
    skipped: list[dict[str, str]] = []
    wanted = list(dict.fromkeys(body.media_ids))
    if body.all_videos:
        # the whole library: every video that already has a transcript, newest first, up to the batch limit
        done = await transcribe._transcripts().find(
            {"workspace_id": ctx.workspace_id, "status": {"$in": ["done", "partial"]}}, {"media_id": 1}).sort("created_at", -1).limit(50).to_list(50)
        wanted = list(dict.fromkeys(wanted + [d["media_id"] for d in done]))[:50]
    if not wanted:
        raise HTTPException(status_code=400, detail="Choose the videos to translate captions for.")
    for mid in wanted:
        media = await media_assets.find_one({"id": mid, "workspace_id": ctx.workspace_id})
        if not media:
            skipped.append({"media_id": mid, "reason": "Not in this workspace's library."})
            continue
        tr = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": mid})
        if not tr or tr["status"] not in ("done", "partial"):
            skipped.append({"media_id": mid, "reason": "Has no transcript yet."})
            continue
        targets.append({"media_id": mid, "transcript_id": tr["id"], "language": tr.get("language")})
    if not targets:
        raise HTTPException(status_code=409, detail="None of those videos has a transcript yet, so there is nothing to translate.")
    await assert_ai_budget_available(ctx.workspace_id)
    try:
        await pipeline_runs.assert_capacity(ctx.workspace_id)
        run = await pipeline_runs.create_run(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind="video", title=f"Translating captions into {name}",
            steps_total=max(1, len(targets)), ref={"language": code}, href="/dashboard/pipelines/video", active_key=f"video-captions:{code}",
        )
    except pipeline_runs.RunAlreadyActive as exc:
        return {"run": pipeline_runs.public(exc.existing), "skipped": skipped, "videos": 0}

    async def work(row: dict) -> dict:
        reporter = pipeline_runs.StepReporter(row["id"])
        done = 0
        for n, t in enumerate(targets):
            await reporter.step(f"Translating captions {n + 1} of {len(targets)}")
            words = await transcribe.all_words(t["transcript_id"])
            segs = P.segments_from_words(words)
            if not segs:
                continue
            lines, rep = await D.translate(segs, target_name=name, target_code=code, register_note=body.register_note, glossary=body.glossary, tighten=False)
            cues = [{"start_ms": s["start_ms"], "end_ms": s["end_ms"], "text": ln["text"]} for s, ln in zip(segs, lines) if ln["text"]]
            await get_db()["video_caption_tracks"].update_one(
                {"workspace_id": ctx.workspace_id, "media_id": t["media_id"], "language": code},
                {"$set": {"workspace_id": ctx.workspace_id, "media_id": t["media_id"], "language": code, "language_name": name, "cues": cues,
                          "failed": rep["failed"], "created_at": _now()}}, upsert=True)
            done += 1
        return {"data": {"videos": done}, "href": "/dashboard/pipelines/video"}

    pipeline_runs.start(run, work, interrupt_on_cancel=True)
    return {"run": pipeline_runs.public(run), "skipped": skipped, "videos": len(targets)}


def _srt_time(ms: int) -> str:
    h, rem = divmod(int(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


@router.get("/captions-ready")
@limiter.limit("60/minute")
async def captions_ready(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """How many of this workspace's videos already have a transcript, which is what a whole-library caption translation works on (the
    newest 50). The Localize card reads it to say what it will do, and to stay out of the way when there is nothing to translate."""
    ids = await transcribe._transcripts().distinct("media_id", {"workspace_id": ctx.workspace_id, "status": {"$in": ["done", "partial"]}})
    return {"videos": min(len(ids), 50), "total": len(ids), "limit": 50}


@router.get("/captions/{media_id}")
@limiter.limit("120/minute")
async def caption_languages(request: Request, media_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    rows = await get_db()["video_caption_tracks"].find({"workspace_id": ctx.workspace_id, "media_id": media_id}, {"cues": 0}).to_list(50)
    return {"languages": [{"language": r["language"], "name": r.get("language_name"), "failed_lines": len(r.get("failed") or [])} for r in rows]}


@router.get("/captions/{media_id}/{language}.srt")
@limiter.limit("60/minute")
async def caption_file(request: Request, media_id: str, language: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> Response:
    row = await get_db()["video_caption_tracks"].find_one({"workspace_id": ctx.workspace_id, "media_id": media_id, "language": language})
    if not row or not row.get("cues"):
        raise HTTPException(status_code=404, detail="There are no translated captions for that language yet.")
    out = io.StringIO()
    for n, c in enumerate(row["cues"], 1):
        out.write(f"{n}\n{_srt_time(c['start_ms'])} --> {_srt_time(c['end_ms'])}\n{c['text']}\n\n")
    return Response(content=out.getvalue(), media_type="application/x-subrip",
                    headers={"Content-Disposition": f'attachment; filename="captions.{language}.srt"'})
