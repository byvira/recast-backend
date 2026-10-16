"""A video made from a description. First a draft: a script split into scenes, with a few stock clips suggested for each (the only cost is
one script request; stock search is free). The member reads it, changes the words or the footage, and only then asks for the video:
the chosen clips are saved to the library with their licences, each scene is spoken in the member's own voice, and a project opens in the
editor with the scenes in order. Nothing is spoken or downloaded before that second step."""
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.v1.video_create import _Base, _budget, _finish, _language
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.db.mongo import brand_profiles, media_assets
from app.models.media import ORIGIN_AI, MediaAsset, MediaKind, MediaSource
from app.shared import pipeline_runs
from app.video import builders, edl, service
from app.video.create import generate as G
from app.video.create import voice as voice_mod
from app.video.stock import providers, rank
from app.video.stock import service as stock

logger = logging.getLogger(__name__)


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])

Size = Literal["vertical", "portrait", "square", "landscape"]
SHAPE: dict[str, str] = {"vertical": "tall", "portrait": "tall", "square": "square", "landscape": "wide"}
OPTIONS_PER_SCENE = 4
STOCK_KEEP = ("provider", "id", "page_url", "preview_url", "poster_url", "width", "height", "duration_s", "author", "author_url", "license")


class DraftBody(_Base):
    prompt: str = Field(..., min_length=8, max_length=2000)
    size: Size = "vertical"
    target_seconds: float = Field(45, ge=15, le=180)


@router.post("/draft")
@limiter.limit("6/minute")
async def draft(request: Request, body: DraftBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """A script in scenes for the described video, with stock suggestions for each scene. Nothing is saved to the library and nothing is
    spoken; the member decides what to keep."""
    from app.shared.llm import set_usage_workspace

    await _budget(ctx)
    set_usage_workspace(ctx.workspace_id)
    voice = await voice_mod.load(ctx.workspace_id, body.brand_id)
    result, meta = await G.script_from_text(body.prompt.strip(), target_seconds=body.target_seconds, language=_language(body.language), voice=voice)
    scenes = result.get("beats") or []
    if not scenes:
        raise HTTPException(status_code=502, detail="Couldn't write a script from that. Try describing the video a little differently.")
    connected = providers.configured()
    out: list[dict[str, Any]] = []
    for scene in scenes[: builders.MAX_BEATS]:
        options: list[dict[str, Any]] = []
        query = (scene.get("visual") or scene.get("on_screen") or "").strip()
        if connected and query:
            found = await providers.search(query, per_page=8)
            ranked = rank.rank(found["clips"], f"{query} {scene['narration']}", want=SHAPE[body.size], min_seconds=2.0, query=query)
            options = [{k: c.get(k) for k in STOCK_KEEP} for c in ranked[:OPTIONS_PER_SCENE]]
        out.append({"narration": scene["narration"], "on_screen": scene.get("on_screen", ""), "visual": scene.get("visual", ""),
                    "seconds": scene["seconds"], "options": options})
    saved = await _finish(ctx, "prompt_script", {"beats": out, "total_seconds": result.get("total_seconds")}, meta, voice,
                          summary="Wrote a script in scenes for a video made from a description.")
    return {
        "id": saved["id"], "scenes": out, "total_seconds": result.get("total_seconds"), "connected": bool(connected), "size": body.size,
        "complete": saved["complete"], "problems": (saved["meta"].get("problems") or [])[:4],
        "note": None if connected else "No stock footage library is connected on this server yet, so no footage can be suggested.",
    }


class ClipRef(BaseModel):
    provider: str = Field(..., min_length=2, max_length=20)
    id: str = Field(..., min_length=1, max_length=80)


class SceneBody(BaseModel):
    narration: str = Field(..., min_length=1, max_length=1200)
    on_screen: str = Field("", max_length=80)
    clip: Optional[ClipRef] = None


class MakeBody(_Base):
    scenes: list[SceneBody] = Field(..., min_length=1, max_length=builders.MAX_BEATS)
    size: Size = "vertical"
    title: str = Field("", max_length=200)
    captions: bool = True
    prompt: str = Field("", max_length=2000)


@router.post("/make", status_code=202)
@limiter.limit("4/minute")
async def make(request: Request, body: MakeBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Saves the chosen clips, speaks every scene and opens a project with the scenes in order. It runs in the activity dock and ends with
    the project's address; it does not render the video."""
    from app.api.v1.video_narrate import _voice_tools, resolve_language
    from app.pipelines.media.tts_generation import narration_blocker
    from app.video import governance

    if not await governance.video_enabled(ctx.workspace_id):
        raise HTTPException(status_code=403, detail=governance.ACCESS_OFF)
    make_speak, store, forget, voice_settings = await _voice_tools(ctx)
    code, name = resolve_language(body.language) if body.language else ("en", "English")
    blocker = narration_blocker(name, voice_settings)
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)
    brand = await brand_profiles.find_one({"id": body.brand_id, "workspace_id": ctx.workspace_id}) if body.brand_id else None
    colors = ((brand or {}).get("visual_identity") or {}).get("colors") or {}
    try:
        await pipeline_runs.assert_capacity(ctx.workspace_id)
        run = await pipeline_runs.create_run(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind="video", title="Making a video from a description",
            steps_total=len(body.scenes) * 2 + 2, href="/dashboard/pipelines/video", active_key=f"video-prompt:{ctx.workspace_id}",
        )
    except pipeline_runs.RunAlreadyActive as exc:
        return pipeline_runs.public(exc.existing)

    async def work(row: dict) -> dict:
        from app.api.v1.video_projects import insert_project
        from app.pipelines.media import duration as audio_duration
        from app.pipelines.media.speech_chunks import split_for_speech
        from app.shared.storage import ContentType as UploadContentType

        reporter = pipeline_runs.StepReporter(row["id"])
        speak = make_speak(code, None)
        prepare = getattr(speak, "prepare", None)
        if prepare is not None:
            await reporter.step("Getting the voice ready")
            await prepare([(piece, None) for sc in body.scenes for piece in split_for_speech(sc.narration)])
        beats: list[dict[str, Any]] = []
        missing_footage = 0
        stored: list[str] = []
        voice_ids: list[str] = []
        try:
            for n, sc in enumerate(body.scenes):
                clip_info: Optional[dict[str, Any]] = None
                if sc.clip:
                    await reporter.step(f"Getting footage for scene {n + 1} of {len(body.scenes)}")
                    try:
                        media = await stock.use_clip(workspace_id=ctx.workspace_id, user_id=ctx.user_id, provider=sc.clip.provider, clip_id=sc.clip.id,
                                                     query=sc.on_screen or sc.narration[:60])
                        if media.duration_s:
                            clip_info = {"media_id": media.id, "seconds": float(media.duration_s)}
                        else:
                            missing_footage += 1
                    except HTTPException as exc:
                        logger.warning("Stock clip %s/%s could not be used: %s", sc.clip.provider, sc.clip.id, exc.detail)
                        missing_footage += 1
                await reporter.step(f"Speaking scene {n + 1} of {len(body.scenes)}")
                parts: list[dict[str, Any]] = []
                for piece in split_for_speech(sc.narration):
                    audio = await speak(piece, None)
                    length = audio_duration.audio_duration_seconds(audio) if audio else None
                    if not audio or not length:
                        raise service.RenderFailed("The voice could not read the script. Check the voice settings and try again.")
                    url = await store(audio)
                    stored.append(url)
                    vm = MediaAsset(
                        id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.AUDIO, url=url, mime_type="audio/mpeg",
                        source=MediaSource.SYNTHESIZED, created_by=ctx.user_id, created_at=datetime.now(timezone.utc), size_bytes=len(audio),
                        duration_s=length,
                    )
                    await media_assets.insert_one(vm.model_dump())
                    voice_ids.append(vm.id)
                    parts.append({"media_id": vm.id, "seconds": length})
                beats.append({"text": sc.narration, "on_screen": sc.on_screen, "voice": parts, "clip": clip_info})
            await reporter.step("Putting the scenes together")
            try:
                edit = builders.from_beats(beats=beats, size=body.size, accent=colors.get("accent") or "#38bdf8",
                                           background=colors.get("secondary") or "#0f172a", captions=body.captions)
            except edl.EditError as exc:
                raise service.RenderFailed(str(exc))
            project = await insert_project(
                ctx.workspace_id, ctx.user_id, body.title.strip() or "Video from a description", edit,
                {"kind": "prompt", "scenes": len(beats), "prompt": body.prompt[:300], "origin": ORIGIN_AI},
            )
            stored.clear()  # the voice files now belong to the library
        except BaseException:
            if voice_ids:
                await media_assets.delete_many({"workspace_id": ctx.workspace_id, "id": {"$in": voice_ids}})
            raise
        finally:
            for url in stored:
                try:
                    await forget(url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not remove a temporary voice file: %s", exc)
        return {"data": {"project_id": project["id"], "missing_footage": missing_footage}, "href": f"/dashboard/editor/video/{project['id']}"}

    pipeline_runs.start(run, work, interrupt_on_cancel=True)
    return pipeline_runs.public(run)
