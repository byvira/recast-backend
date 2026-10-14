"""Video projects: the saved edit list behind the editor and every one-click video.

A project keeps a list of edits that refer to the member's library by id. Saving makes a version, nothing is rendered until
Render, and a render is a job on the worker that also shows in the activity dock and the Activity Log like any other long
work. Every response is sent fresh (never cached), so a screen always shows the state as it is now.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import audio_assets, brand_profiles, get_db, media_assets
from app.shared import pipeline_runs as run_layer
from app.video import builders, edl, jobs
from app.video.origin import LABELS, origin_of, summarise

logger = logging.getLogger(__name__)


async def no_store(response: Response) -> None:
    """Video screens poll while work is going on, so nothing here may come from a cache."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])

AUTOSAVES_KEPT = 20
PRESETS: dict[str, dict[str, Any]] = {
    "standard": {"crf": 21, "preset": "veryfast", "audio_bitrate": "192k"},
    "high": {"crf": 18, "preset": "faster", "audio_bitrate": "256k"},
    "small": {"crf": 27, "preset": "veryfast", "audio_bitrate": "128k"},
}
LOUDNESS = (-14.0, -16.0, -23.0, -24.0)


def _projects():
    return get_db()["video_projects"]


def _versions():
    return get_db()["video_project_versions"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if not isinstance(value, datetime):
        return None
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _summary(doc: dict) -> dict:
    return {
        "id": doc["id"], "title": doc.get("title", ""), "version": doc["version"], "status": doc.get("status", "draft"),
        "width": doc["edit"]["width"], "height": doc["edit"]["height"], "duration_f": doc["edit"]["duration_f"],
        "fps": doc["edit"]["fps"], "created_from": doc.get("created_from"), "last_render": doc.get("last_render"),
        "created_at": _iso(doc.get("created_at")), "updated_at": _iso(doc.get("updated_at")),
    }


async def _media_map(workspace_id: str, ids: set[str]) -> dict[str, dict]:
    """The library items a project uses, as the editor and the renderer need them. Anything not in this workspace is absent."""
    if not ids:
        return {}
    rows = await media_assets.find({"workspace_id": workspace_id, "id": {"$in": list(ids)}}).to_list(length=len(ids))
    out: dict[str, dict] = {}
    for r in rows:
        kind = r.get("kind")
        kind = kind.value if hasattr(kind, "value") else kind
        origin = origin_of(r)
        out[r["id"]] = {
            "id": r["id"], "url": r.get("url"), "kind": kind, "duration_s": r.get("duration_s"), "width": r.get("width"),
            "height": r.get("height"), "origin": origin, "origin_label": LABELS.get(origin, ""),
            "contains_ai": bool(r.get("contains_ai")) or origin == "ai", "poster_url": r.get("poster_url"),
            "has_audio": (r.get("probe") or {}).get("audio") is not None if r.get("probe") else None,
        }
    return out


async def _load(workspace_id: str, project_id: str) -> dict:
    doc = await _projects().find_one({"id": project_id, "workspace_id": workspace_id, "deleted": {"$ne": True}})
    if not doc:
        raise HTTPException(status_code=404, detail="Video project not found.")
    return doc


async def _open(doc: dict) -> dict:
    media = await _media_map(doc["workspace_id"], edl.media_ids(doc["edit"]))
    origin = summarise(m["origin"] for m in media.values())
    return {**_summary(doc), "edit": doc["edit"], "media": media, "origin": {**origin, "label": LABELS.get(origin["origin"], "")}}


def _check(edit_raw: Any) -> dict:
    try:
        return edl.validate(edit_raw)
    except edl.EditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _check_media(workspace_id: str, edit: dict) -> None:
    wanted = edl.media_ids(edit)
    have = set((await _media_map(workspace_id, wanted)).keys())
    if wanted - have:
        raise HTTPException(status_code=400, detail="A clip uses a file that is not in this workspace's library.")


async def _write_version(doc: dict, kind: str, label: str, user_id: str) -> None:
    await _versions().insert_one({
        "id": str(uuid4()), "project_id": doc["id"], "workspace_id": doc["workspace_id"], "version": doc["version"],
        "edit": doc["edit"], "title": doc.get("title", ""), "kind": kind, "label": label, "created_by": user_id, "created_at": _now(),
    })
    if kind == "autosave":
        old = await _versions().find({"project_id": doc["id"], "kind": "autosave"}, {"id": 1}).sort("version", -1).skip(AUTOSAVES_KEPT).to_list(200)
        if old:
            await _versions().delete_many({"id": {"$in": [o["id"] for o in old]}})


# ── creating ─────────────────────────────────────────────────────────────────────

class CreateBody(BaseModel):
    source: Literal["blank", "video", "recording", "pictures"] = "blank"
    title: str = Field("", max_length=200)
    size: Optional[Literal["square", "vertical", "landscape", "portrait"]] = None
    media_id: Optional[str] = None
    audio_asset_id: Optional[str] = None
    start_s: float = Field(0.0, ge=0)
    end_s: Optional[float] = Field(None, gt=0)
    # for a slideshow from pictures
    picture_media_ids: list[str] = Field(default_factory=list, max_length=60)
    seconds_per_picture: float = Field(3.0, ge=1.0, le=30.0)
    voice_media_id: Optional[str] = None
    music_media_id: Optional[str] = None


@router.post("", status_code=201)
@limiter.limit("30/minute")
async def create_project(request: Request, body: CreateBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict:
    created_from: dict[str, Any] = {"kind": body.source}
    title = body.title.strip()
    try:
        if body.source == "blank":
            edit = builders.blank_project(body.size or "landscape")
        elif body.source == "video":
            if not body.media_id:
                raise HTTPException(status_code=400, detail="Choose a video from the library.")
            media = await media_assets.find_one({"id": body.media_id, "workspace_id": ctx.workspace_id, "kind": "video"})
            if not media:
                raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
            edit = builders.from_video(media, size=body.size)
            created_from["media_id"] = body.media_id
            title = title or "Untitled video"
        elif body.source == "pictures":
            if not body.picture_media_ids:
                raise HTTPException(status_code=400, detail="Choose at least one picture.")
            wanted = set(body.picture_media_ids) | {m for m in (body.voice_media_id, body.music_media_id) if m}
            found = {r["id"]: r for r in await media_assets.find({"workspace_id": ctx.workspace_id, "id": {"$in": list(wanted)}}).to_list(len(wanted))}
            missing = wanted - set(found)
            if missing:
                raise HTTPException(status_code=404, detail="A picture or sound you chose is not in this workspace's library.")
            for mid in body.picture_media_ids:
                kind = found[mid].get("kind")
                if (kind.value if hasattr(kind, "value") else kind) != "image":
                    raise HTTPException(status_code=400, detail="Only pictures can go in a slideshow.")
            voice = found.get(body.voice_media_id) if body.voice_media_id else None
            if body.voice_media_id and (voice.get("kind").value if hasattr(voice.get("kind"), "value") else voice.get("kind")) != "audio":
                raise HTTPException(status_code=400, detail="The voice has to be an audio file.")
            edit = builders.from_pictures(
                picture_media_ids=body.picture_media_ids, size=body.size or "vertical", seconds_per_picture=body.seconds_per_picture,
                title=title, voice_media_id=body.voice_media_id, voice_seconds=float(voice.get("duration_s") or 0) if voice else None,
                music_media_id=body.music_media_id,
            )
            created_from.update({"picture_count": len(body.picture_media_ids)})
            title = title or "Slideshow"
        else:
            if not body.audio_asset_id:
                raise HTTPException(status_code=400, detail="Choose a recording.")
            doc = await audio_assets.find_one({"id": body.audio_asset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}})
            if not doc:
                raise HTTPException(status_code=404, detail="That recording was not found.")
            voice_id = doc.get("approved_master_media_id") or doc.get("media_id")
            voice = await media_assets.find_one({"id": voice_id, "workspace_id": ctx.workspace_id}) if voice_id else None
            if not voice:
                raise HTTPException(status_code=400, detail="This recording has no audio file to turn into a video.")
            from app.api.v1.audio_assets import _master_transcript

            length = float(voice.get("duration_s") or 0)
            end_s = min(body.end_s, length) if body.end_s and length else (body.end_s or length)
            if end_s <= body.start_s:
                raise HTTPException(status_code=400, detail="The end time has to be after the start time.")
            brand = await brand_profiles.find_one({"id": doc["brand_id"], "workspace_id": ctx.workspace_id})
            colors = ((brand or {}).get("visual_identity") or {}).get("colors") or {}
            edit = builders.from_recording(
                voice_media_id=voice["id"], seconds=end_s - body.start_s, words=[
                    {"word": w["word"], "start_s": w["start_s"], "end_s": w["end_s"]} for w in _master_transcript(doc)
                ], size=body.size or "vertical", title=title or doc.get("title", ""), accent=colors.get("accent") or "#38bdf8",
                background=colors.get("secondary") or "#0f172a", start_s=body.start_s,
            )
            created_from.update({"audio_asset_id": body.audio_asset_id, "start_s": body.start_s, "end_s": end_s})
            title = title or doc.get("title", "") or "Untitled video"
    except edl.EditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": ctx.workspace_id, "title": title or "Untitled video", "edit": edit, "initial_edit": edit,
        "version": 1, "status": "draft", "created_from": created_from, "created_by": ctx.user_id, "created_at": now, "updated_at": now,
        "last_render": None,
    }
    await _projects().insert_one(dict(doc))
    await _write_version(doc, "save", "Start", ctx.user_id)
    return await _open(doc)


@router.get("")
@limiter.limit("120/minute")
async def list_projects(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    rows = await _projects().find({"workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}).sort("updated_at", -1).to_list(length=100)
    return {"projects": [_summary(r) for r in rows]}


@router.get("/jobs/{job_id}")
@limiter.limit("240/minute")
async def get_job(request: Request, job_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    job = await jobs.get_job(ctx.workspace_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Render not found.")
    return job


@router.post("/jobs/{job_id}/cancel")
@limiter.limit("60/minute")
async def cancel_job(request: Request, job_id: str, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict:
    try:
        return await jobs.cancel_job(ctx.workspace_id, job_id)
    except jobs.JobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


@router.get("/{project_id}")
@limiter.limit("240/minute")
async def get_project(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    return await _open(await _load(ctx.workspace_id, project_id))


# ── saving, versions ─────────────────────────────────────────────────────────────

class SaveBody(BaseModel):
    edit: dict[str, Any]
    base_version: int = Field(..., ge=1)
    autosave: bool = False
    title: Optional[str] = Field(None, max_length=200)


@router.put("/{project_id}")
@limiter.limit("240/minute")
async def save_project(
    request: Request, project_id: str, body: SaveBody, ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    doc = await _load(ctx.workspace_id, project_id)
    edit = _check(body.edit)
    await _check_media(ctx.workspace_id, edit)
    title = (body.title if body.title is not None else doc.get("title", "")).strip() or "Untitled video"
    now = _now()
    updated = await _projects().find_one_and_update(
        {"id": project_id, "workspace_id": ctx.workspace_id, "version": body.base_version},
        {"$set": {"edit": edit, "title": title, "updated_at": now}, "$inc": {"version": 1}},
        return_document=True,
    )
    if not updated:
        current = await _projects().find_one({"id": project_id, "workspace_id": ctx.workspace_id}, {"version": 1})
        raise HTTPException(status_code=409, detail={
            "code": "VERSION_CONFLICT", "message": "This project was changed somewhere else. Reload to see the latest.",
            "current_version": (current or {}).get("version"),
        })
    await _write_version(updated, "autosave" if body.autosave else "save", "Autosave" if body.autosave else "Saved", ctx.user_id)
    return await _open(updated)


@router.get("/{project_id}/versions")
@limiter.limit("120/minute")
async def list_versions(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    await _load(ctx.workspace_id, project_id)
    rows = await _versions().find({"project_id": project_id, "workspace_id": ctx.workspace_id}, {"edit": 0}).sort("version", -1).to_list(200)
    return {"versions": [{
        "version": r["version"], "kind": r["kind"], "label": r.get("label", ""), "title": r.get("title", ""),
        "created_at": _iso(r.get("created_at")),
    } for r in rows]}


async def _replace_edit(ctx: WorkspaceContext, doc: dict, edit: dict, kind: str, label: str) -> dict:
    # what is there now is kept as a version first, so nothing is ever lost by going back or starting again
    await _write_version(doc, "save", f"Before {label.lower()}", ctx.user_id)
    updated = await _projects().find_one_and_update(
        {"id": doc["id"], "workspace_id": ctx.workspace_id, "version": doc["version"]},
        {"$set": {"edit": edit, "updated_at": _now()}, "$inc": {"version": 1}}, return_document=True,
    )
    if not updated:
        raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "message": "This project was changed somewhere else. Try again."})
    await _write_version(updated, kind, label, ctx.user_id)
    return await _open(updated)


@router.post("/{project_id}/versions/{version}/restore")
@limiter.limit("30/minute")
async def restore_version(
    request: Request, project_id: str, version: int, ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    doc = await _load(ctx.workspace_id, project_id)
    old = await _versions().find_one({"project_id": project_id, "workspace_id": ctx.workspace_id, "version": version})
    if not old:
        raise HTTPException(status_code=404, detail="That version was not found.")
    return await _replace_edit(ctx, doc, edl.validate(old["edit"]), "restore", "Restored")


@router.post("/{project_id}/reset")
@limiter.limit("30/minute")
async def reset_project(request: Request, project_id: str, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict:
    doc = await _load(ctx.workspace_id, project_id)
    return await _replace_edit(ctx, doc, edl.validate(doc.get("initial_edit") or doc["edit"]), "reset", "Started again")


@router.delete("/{project_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_project(request: Request, project_id: str, ctx: WorkspaceContext = Depends(require("edit_content"))) -> Response:
    await _load(ctx.workspace_id, project_id)
    await _projects().update_one({"id": project_id, "workspace_id": ctx.workspace_id}, {"$set": {"deleted": True, "updated_at": _now()}})
    return Response(status_code=204)


# ── rendering ────────────────────────────────────────────────────────────────────

class RenderBody(BaseModel):
    preset: Literal["standard", "high", "small"] = "standard"
    lufs: float = -14.0


@router.post("/{project_id}/render", status_code=202)
@limiter.limit("12/minute")
async def render_project(
    request: Request, project_id: str, body: RenderBody, ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    if body.lufs not in LOUDNESS:
        raise HTTPException(status_code=400, detail="Loudness must be one of: " + ", ".join(str(int(v)) for v in LOUDNESS) + ".")
    doc = await _load(ctx.workspace_id, project_id)
    media = await _media_map(ctx.workspace_id, edl.media_ids(doc["edit"]))
    try:
        compiled = edl.to_job(doc["edit"], media, {**PRESETS[body.preset], "lufs": body.lufs})
    except edl.EditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        job = await jobs.enqueue(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, inputs=compiled["inputs"], edit=compiled["edit"], output=compiled["output"],
            label=f"Rendering {doc.get('title') or 'video'}", project_id=project_id, show_in_activity=True,
            href=f"/dashboard/pipelines/video?project={project_id}", origin=summarise(m["origin"] for m in media.values()),
            idem_key=f"proj:{project_id}:v{doc['version']}:{body.preset}:{body.lufs}",
        )
    except jobs.JobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    except run_layer.RunAlreadyActive:
        raise HTTPException(status_code=409, detail="This video is already being rendered. It will show in your videos when it is ready.")
    await _projects().update_one(
        {"id": project_id, "workspace_id": ctx.workspace_id},
        {"$set": {"last_render": {"job_id": job["id"], "run_id": job.get("run_id"), "preset": body.preset, "version": doc["version"], "at": _now()}}},
    )
    return job


@router.get("/{project_id}/renders")
@limiter.limit("120/minute")
async def list_renders(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    await _load(ctx.workspace_id, project_id)
    rows = await get_db()["video_jobs"].find({"workspace_id": ctx.workspace_id, "project_id": project_id}).sort("created_at", -1).to_list(20)
    return {"renders": [jobs.public(r) for r in rows]}
