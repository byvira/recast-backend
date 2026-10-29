"""What is in a video: outline, chapters, moments worth clipping, the silence and filler cut list, the shot log, and files that carry
them into other tools. Everything is read from the video's real transcript and preview; suggestions are never applied by themselves."""
import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db, media_assets
from app.shared import pipeline_runs
from app.video import transcribe
from app.video.analysis import analyze as A
from app.video.analysis import silence as S

logger = logging.getLogger(__name__)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) and value.tzinfo is None else value


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])


def _analyses():
    return get_db()["video_analysis"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _media(ctx: WorkspaceContext, media_id: str) -> dict:
    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    kind = (media or {}).get("kind")
    kind = kind.value if hasattr(kind, "value") else kind
    if not media or kind not in ("video", "audio"):
        raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
    return media


async def _transcript(ctx: WorkspaceContext, media_id: str) -> dict:
    doc = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not doc or doc["status"] not in ("done", "partial"):
        raise HTTPException(status_code=409, detail="Write out what was said in this video first. Everything here is read from the transcript.")
    return doc


def _public(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return None
    out = {k: doc.get(k) for k in ("id", "media_id", "status", "parts", "outline", "chapters", "moments", "note", "language", "stats", "error")}
    for f in ("created_at", "updated_at"):
        v = doc.get(f)
        out[f] = (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z") if v else None
    return out


class AnalyzeBody(BaseModel):
    parts: list[Literal["outline", "moments"]] = Field(default_factory=lambda: ["outline", "moments"])
    platform: Optional[str] = Field(None, max_length=30)
    min_s: float = Field(20.0, ge=5, le=300)
    max_s: float = Field(60.0, ge=10, le=600)
    count: int = Field(8, ge=1, le=20)


@router.post("/{media_id}", status_code=202)
@limiter.limit("10/minute")
async def start(
    request: Request, media_id: str, body: AnalyzeBody = AnalyzeBody(), ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict[str, Any]:
    from app.agents.supervisor.service import assert_ai_budget_available

    await _media(ctx, media_id)
    tr = await _transcript(ctx, media_id)
    if body.max_s < body.min_s:
        raise HTTPException(status_code=400, detail="The longest clip has to be at least as long as the shortest.")
    await assert_ai_budget_available(ctx.workspace_id)
    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": ctx.workspace_id, "media_id": media_id, "transcript_id": tr["id"], "status": "running",
        "parts": body.parts, "language": tr.get("language") or "en", "outline": None, "chapters": [], "moments": [], "note": None,
        "stats": {}, "error": None, "created_at": now, "updated_at": now,
    }
    # one analysis per video: starting again replaces the last one, but keeps the member's choices on moments that still exist
    previous = await _analyses().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if previous and previous.get("status") == "running":
        age = _now() - (_aware(previous.get("updated_at")) or _now())
        if age < timedelta(minutes=45):
            return {"analysis": _public(previous), "run": None}
        # nothing has been heard from it for a long time: it was cut off (a restart, a cancel), so it can be started again
        previous = {**previous, "status": "failed", "error": "This was interrupted. Please try again."}
    run = None
    try:
        await pipeline_runs.assert_capacity(ctx.workspace_id)
        run = await pipeline_runs.create_run(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind="video", title="Reading a video", steps_total=20,
            ref={"media_id": media_id, "analysis_id": doc["id"]}, href=f"/dashboard/pipelines/video?media={media_id}",
            active_key=f"video-analysis:{media_id}",
        )
    except pipeline_runs.RunAlreadyActive as exc:
        return {"analysis": _public(doc), "run": pipeline_runs.public(exc.existing)}
    # the earlier result is replaced only now that the run exists, and it is put back if this one fails or is stopped
    try:
        await _analyses().replace_one({"workspace_id": ctx.workspace_id, "media_id": media_id}, doc, upsert=True)
    except BaseException:
        await pipeline_runs._finish(run, "failed", error="The video could not be read. Please try again.")
        raise

    async def restore_previous() -> None:
        if previous:
            old = {k: v for k, v in previous.items() if k != "_id"}
            await _analyses().replace_one({"workspace_id": ctx.workspace_id, "media_id": media_id, "id": doc["id"]}, old)

    async def work(row: dict) -> dict:
        from app.billing import credits
        from app.shared.llm import set_usage_workspace

        reporter = pipeline_runs.StepReporter(row["id"])
        set_usage_workspace(ctx.workspace_id)
        try:
            words = await transcribe.all_words(tr["id"])
            # 1 credit for each 10 minutes of video read, charged when the reading is done
            async with credits.spend(
                ctx.workspace_id, ctx.user_id, "video_analysis", float(tr["duration_s"] or 60) / 60.0, step=f"analysis:{media_id}",
            ):
                result = await A.analyze(
                    words, language=doc["language"], duration_s=float(tr["duration_s"]), parts=tuple(body.parts), min_s=body.min_s,
                    max_s=body.max_s, count=body.count, platform=body.platform, reporter=reporter,
                )
            kept = {m["start_ms"]: m["state"] for m in ((previous or {}).get("moments") or []) if m.get("state") != "suggested"}
            for m in result["moments"]:
                if m["start_ms"] in kept:
                    m["state"] = kept[m["start_ms"]]
            await _analyses().update_one({"id": doc["id"]}, {"$set": {
                "status": "done", "outline": result["outline"], "chapters": result["chapters"], "moments": result["moments"],
                "note": result.get("note"), "stats": {"sentences": result["sentences"], "windows": result["windows"], "cache_hits": result.get("cache_hits", 0)},
                "updated_at": _now(),
            }})
        except BaseException as exc:  # noqa: BLE001 - a cancel or a restart ends the work without an Exception
            logger.warning("Analysis %s did not finish: %s", doc["id"], type(exc).__name__)
            try:
                await asyncio.shield(restore_previous())
                if not previous:
                    await asyncio.shield(_analyses().update_one({"id": doc["id"]}, {"$set": {
                        "status": "failed", "error": "The video could not be read right now. Please try again.", "updated_at": _now()}}))
            except Exception as inner:  # noqa: BLE001
                logger.warning("Could not tidy up analysis %s: %s", doc["id"], inner)
            raise
        from app.video import activity

        await activity.emit("analysis_done", ctx.workspace_id, ctx.user_id, doc["id"], media_id=media_id)
        return {"data": {"media_id": media_id}, "href": f"/dashboard/pipelines/video?media={media_id}"}

    pipeline_runs.start(run, work, interrupt_on_cancel=True)
    return {"analysis": _public(doc), "run": pipeline_runs.public(run)}


@router.get("/{media_id}")
@limiter.limit("240/minute")
async def get_analysis(request: Request, media_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    await _media(ctx, media_id)
    return {"analysis": _public(await _analyses().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id}))}


class MomentBody(BaseModel):
    state: Literal["suggested", "accepted", "rejected"]


@router.post("/{media_id}/moments/{moment_id}")
@limiter.limit("120/minute")
async def set_moment(
    request: Request, media_id: str, moment_id: str, body: MomentBody, ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict[str, Any]:
    await _media(ctx, media_id)
    res = await _analyses().find_one_and_update(
        {"workspace_id": ctx.workspace_id, "media_id": media_id, "moments.id": moment_id},
        {"$set": {"moments.$.state": body.state, "updated_at": _now()}}, return_document=True,
    )
    if not res:
        raise HTTPException(status_code=404, detail="That moment was not found. Read the video again.")
    return {"analysis": _public(res)}


async def _judge(language: str):
    async def judge(items: list[dict[str, Any]]) -> dict[int, bool]:
        text = "\n".join(f"{i['id']}: word \"{i['word']}\" in: {i['context']}" for i in items)
        raw, _ = await A.cached_call("video/filler_judge", {"language": language, "items_text": text}, caller=A._default_caller, pacer=A.Pacer())
        out: dict[int, bool] = {}
        for d in raw.get("decisions") or []:
            try:
                out[int(d.get("id"))] = bool(d.get("remove"))
            except (TypeError, ValueError, AttributeError):
                continue
        return out
    return judge


@router.get("/{media_id}/cuts")
@limiter.limit("60/minute")
async def cuts(
    request: Request, media_id: str, pause_ms: int = Query(600, ge=300, le=3000), fillers: bool = True, judge: bool = False,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """Where this talk can be tightened: pauses, fillers and silences. A preview only; nothing is cut until it is applied."""
    if judge:
        # the model's second opinion on fillers is a paid call: it needs the right to edit, the owner's pause off and budget left
        from app.agents.supervisor.service import assert_ai_budget_available, assert_generation_allowed
        from app.core.workspace import assert_permission

        from app.shared.llm import set_usage_workspace

        assert_permission(ctx.member, "edit_content")
        await assert_generation_allowed(ctx.workspace_id)
        await assert_ai_budget_available(ctx.workspace_id)
        set_usage_workspace(ctx.workspace_id)
    media = await _media(ctx, media_id)
    tr = await _transcript(ctx, media_id)
    words = await transcribe.all_words(tr["id"])
    items = S.pauses(words, pause_ms=pause_ms)
    if fillers:
        items += await S.fillers(words, tr.get("language"), judge=await _judge(tr.get("language") or "en") if judge else None)
    wave = (media.get("preview") or {}).get("waveform_url")
    if wave:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20) as client:
                data = (await client.get(wave)).json()
            items += S.silences_from_peaks(data.get("peaks") or [], int(data.get("per_second") or 20))
        except Exception as exc:  # noqa: BLE001 - the word gaps already cover the speech; the waveform is a bonus
            logger.info("Waveform not used for the cut list of %s: %s", media_id, exc)
    cut = S.cut_list(items)
    return {"pause_ms": pause_ms, "cuts": cut, "count": len(cut), "saved_ms": S.saved_ms(cut), "preview_only": True}


@router.get("/{media_id}/shots")
@limiter.limit("120/minute")
async def shot_log(request: Request, media_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    media = await _media(ctx, media_id)
    meta = ((media.get("preview") or {}).get("meta") or {})
    if media.get("preview_status") != "ready" or "scenes" not in meta:
        raise HTTPException(status_code=409, detail="The preview of this video is still being prepared, so its shots are not known yet.")
    duration = float(media.get("duration_s") or meta.get("duration_s") or 0)
    return {"shots": S.shots(meta["scenes"], duration), "cuts": len(meta["scenes"])}


def _ts(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


@router.get("/{media_id}/export")
@limiter.limit("60/minute")
async def export(
    request: Request, media_id: str, kind: Literal["chapters", "moments", "cuts"], format: Literal["txt", "csv", "json"] = "txt",
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Response:
    """Chapters in the form a video site reads, moments or a cut list as a marker file for other editing tools."""
    await _media(ctx, media_id)
    doc = await _analyses().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    rows: list[dict[str, Any]]
    if kind == "cuts":
        tr = await _transcript(ctx, media_id)
        words = await transcribe.all_words(tr["id"])
        rows = S.cut_list(S.pauses(words) + await S.fillers(words, tr.get("language")))
    else:
        if not doc or doc.get("status") != "done":
            raise HTTPException(status_code=409, detail="Read this video first.")
        rows = list(doc.get("chapters" if kind == "chapters" else "moments") or [])
        if kind == "moments":
            rows = [m for m in rows if m.get("state") != "rejected"]
    if not rows:
        raise HTTPException(status_code=409, detail=f"There are no {kind} for this video yet.")
    if format == "json":
        return Response(content=json.dumps(rows, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{kind}.json"'})
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["start", "end", "title or reason", "kind"])
        for r in rows:
            w.writerow([_ts(r["start_ms"]), _ts(r.get("end_ms", r["start_ms"])), r.get("title") or r.get("reason") or "", r.get("kind") or kind])
        return Response(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'})
    if kind == "chapters":
        text = "\n".join(f"{_ts(r['start_ms'])} {r['title']}" for r in rows)
    else:
        text = "\n".join(f"{_ts(r['start_ms'])} to {_ts(r['end_ms'])}  {r.get('title') or r.get('reason') or ''}" for r in rows)
    return Response(content=text + "\n", media_type="text/plain", headers={"Content-Disposition": f'attachment; filename="{kind}.txt"'})
