"""Video projects: the saved edit list behind the editor and every one-click video.

A project keeps a list of edits that refer to the member's library by id. Saving makes a version, nothing is rendered until
Render, and a render is a job on the worker that also shows in the activity dock and the Activity Log like any other long
work. Every response is sent fresh (never cached), so a screen always shows the state as it is now.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
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


def _approval_view(doc: dict) -> dict:
    """The sign-off on a project: which version was approved and by whom, and whether the project has changed since."""
    a = doc.get("approval") or {}
    version = a.get("version")
    return {"approved_version": version, "approved_at": _iso(a.get("at")), "approved_by": a.get("by_name"),
            "approval_stale": bool(version) and version != doc.get("version")}


def _media_ids(doc: dict) -> list[str]:
    seen: list[str] = []
    for key in ("video", "audio"):
        for c in (doc.get("edit") or {}).get(key, []):
            m = c.get("media_id")
            if m and m not in seen:
                seen.append(m)
    return seen


def _summary(doc: dict) -> dict:
    return {
        **_approval_view(doc), "media_ids": _media_ids(doc),
        "id": doc["id"], "title": doc.get("title", ""), "version": doc["version"], "status": doc.get("status", "draft"),
        "width": doc["edit"]["width"], "height": doc["edit"]["height"], "duration_f": doc["edit"]["duration_f"],
        "fps": doc["edit"]["fps"], "created_from": doc.get("created_from"), "last_render": doc.get("last_render"),
        "created_at": _iso(doc.get("created_at")), "updated_at": _iso(doc.get("updated_at")),
    }


def _preview_fields(row: dict) -> dict:
    """What the editor plays and draws for one item: the small playable copy, the thumbnail strip with its layout, and the waveform
    file. Only what exists is given; an item with no preview yet says so and the editor shows "Preparing preview"."""
    preview = row.get("preview") or {}
    meta = preview.get("meta") or {}
    return {
        "preview_status": row.get("preview_status"),
        "proxy_url": preview.get("proxy_url"),
        "strip_url": preview.get("strip_url"),
        "strip": meta.get("strip") if preview.get("strip_url") else None,
        "waveform_url": preview.get("waveform_url"),
        "waveform": meta.get("waveform") if preview.get("waveform_url") else None,
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
            **_preview_fields(r),
            "font_family": r.get("font_family"),
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
    # who speaks in the first video of the project, so a name card can be placed where each person first appears
    speakers: list[dict] = []
    first_video = next((c["media_id"] for c in doc["edit"].get("video", []) if (media.get(c["media_id"]) or {}).get("kind") == "video"), None)
    if first_video:
        try:
            from app.video import transcribe

            speakers = await transcribe.speaker_starts(doc["workspace_id"], first_video)
        except Exception:  # noqa: BLE001 - the names are a convenience; the project opens without them
            speakers = []
    return {**_summary(doc), "edit": doc["edit"], "media": media, "origin": {**origin, "label": LABELS.get(origin["origin"], "")},
            "speakers": speakers}


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
    fonts = edl.font_ids(edit)
    if fonts:
        found_fonts = await _media_map(workspace_id, fonts)
        if any(not found_fonts.get(i, {}).get("font_family") for i in fonts):
            raise HTTPException(status_code=400, detail="A font used in this project is not one of this workspace's fonts.")
    pictures = {s["media_id"] for s in edit.get("shapes", []) if s.get("media_id")}
    if pictures:
        found = await _media_map(workspace_id, pictures)
        if any(found.get(i, {}).get("kind") != "image" for i in pictures):
            raise HTTPException(status_code=400, detail="A picture over the video must be a picture from the library.")


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
    size: Optional[Literal["square", "vertical", "landscape", "portrait", "vertical_4k", "landscape_4k", "vertical_720", "landscape_720", "square_720", "portrait_720"]] = None
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
    from app.video import governance

    if not await governance.video_enabled(ctx.workspace_id):
        raise HTTPException(status_code=403, detail=governance.ACCESS_OFF)
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


@router.get("/usage")
@limiter.limit("60/minute")
async def video_usage(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """The two meters: render minutes this month and storage, each with its limit on this plan."""
    from app.video import limits

    return await limits.usage(ctx.workspace_id)


@router.get("")
@limiter.limit("120/minute")
async def list_projects(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    rows = await _projects().find({"workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}).sort("updated_at", -1).to_list(length=100)
    # a picture for each: the poster of its finished video, or of the first clip it uses
    wanted: set[str] = set()
    for r in rows:
        last = (r.get("last_render") or {}).get("media_id")
        first = next((c.get("media_id") for c in r.get("edit", {}).get("video", []) if c.get("media_id")), None)
        wanted.update(m for m in (last, first) if m)
    media = {}
    if wanted:
        for m in await media_assets.find({"workspace_id": ctx.workspace_id, "id": {"$in": list(wanted)}}, {"id": 1, "poster_url": 1, "url": 1, "kind": 1}).to_list(length=len(wanted)):
            media[m["id"]] = m
    out = []
    for r in rows:
        item = _summary(r)
        last_id = (r.get("last_render") or {}).get("media_id")
        first_id = next((c.get("media_id") for c in r.get("edit", {}).get("video", []) if c.get("media_id")), None)
        pick = media.get(last_id) or media.get(first_id) or {}
        item["thumbnail_url"] = pick.get("poster_url") or (pick.get("url") if pick.get("kind") == "image" else None)
        item["made"] = bool(last_id)
        item["made_current"] = bool(last_id) and (r.get("last_render") or {}).get("version") == r.get("version")
        item["linked_posts"] = len(r.get("linked_pieces") or [])
        out.append(item)
    return {"projects": out}


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
    if not body.autosave:
        from app.video import activity

        await activity.version_saved(ctx.workspace_id, ctx.user_id, project_id, int(updated["version"]))
    return await _open(updated)


class ApprovalBody(BaseModel):
    approved: bool
    #: the version being approved, so approving a page that is out of date is refused instead of signing off something else
    version: Optional[int] = Field(None, ge=1)


@router.put("/{project_id}/approval")
@limiter.limit("30/minute")
async def set_approval(
    request: Request, project_id: str, body: ApprovalBody, ctx: WorkspaceContext = Depends(require("approve_content")),
) -> dict:
    """Sign off the project as it is now, or take the sign-off away. Changing the project afterwards makes the sign-off out of date;
    it does not lock the project."""
    doc = await _load(ctx.workspace_id, project_id)
    if body.approved:
        if body.version is not None and body.version != doc["version"]:
            raise HTTPException(status_code=409, detail="The project changed since you looked at it. Look again, then approve.")
        who = await get_db()["users"].find_one({"id": ctx.user_id}, {"name": 1, "full_name": 1, "email": 1})
        name = (who or {}).get("name") or (who or {}).get("full_name") or (who or {}).get("email") or "A member"
        update = {"$set": {"approval": {"version": doc["version"], "by": ctx.user_id, "by_name": str(name)[:80], "at": _now()}}}
    else:
        update = {"$unset": {"approval": ""}}
    await _projects().update_one({"id": project_id, "workspace_id": ctx.workspace_id}, update)
    fresh = await _load(ctx.workspace_id, project_id)
    await activity.approval_changed(ctx.workspace_id, ctx.user_id, project_id, int(fresh["version"]), body.approved)
    return _approval_view(fresh)


@router.get("/{project_id}/versions")
@limiter.limit("120/minute")
async def list_versions(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    await _load(ctx.workspace_id, project_id)
    rows = await _versions().find({"project_id": project_id, "workspace_id": ctx.workspace_id}, {"edit": 0}).sort("version", -1).to_list(200)
    return {"versions": [{
        "version": r["version"], "kind": r["kind"], "label": r.get("label", ""), "title": r.get("title", ""),
        "created_at": _iso(r.get("created_at")),
    } for r in rows]}


def _aware(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _render_version(job: dict) -> Optional[int]:
    """The project version a render was made from, read from the job's own key (proj:<id>:v<version>:...)."""
    parts = str(job.get("idem_key") or "").split(":")
    if len(parts) >= 3 and parts[0] == "proj" and parts[2].startswith("v") and parts[2][1:].isdigit():
        return int(parts[2][1:])
    return None


def _history_rows(versions: list[dict], renders: list[dict], decisions: list[dict]) -> list[dict]:
    """One time-ordered story of a project: saves, going back, renders and what reviewers answered. Autosaves are left out."""
    rows: list[dict] = []
    for v in versions:
        if v.get("kind") == "autosave":
            continue
        rows.append({"at": v.get("created_at"), "kind": "version", "title": v.get("label") or "Saved", "detail": "", "version": v.get("version"), "job_id": None})
    for j in renders:
        status = j.get("status")
        if status == "done":
            title, detail = "Video made", f"From version {_render_version(j)}." if _render_version(j) else ""
        elif status in ("failed", "cancelled", "blocked"):
            title = {"failed": "Video could not be made", "cancelled": "Video was cancelled", "blocked": "Video was held back"}[status]
            detail = (j.get("error") or "")[:200]
        else:
            continue
        rows.append({"at": j.get("finished_at") or j.get("created_at"), "kind": "render", "title": title, "detail": detail, "version": _render_version(j), "job_id": j.get("id")})
    for d in decisions:
        approved = d.get("outcome") == "approved"
        rows.append({"at": d.get("at"), "kind": "review", "title": f"{d.get('name') or 'A reviewer'} {'approved the video' if approved else 'asked for changes'}",
                     "detail": (d.get("note") or "")[:300], "version": None, "job_id": None})
    rows.sort(key=lambda r: _aware(r["at"]), reverse=True)
    return [{**r, "at": _iso(_aware(r["at"])) if r["at"] else None} for r in rows]


@router.get("/{project_id}/history")
@limiter.limit("60/minute")
async def project_history(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """The whole story of one project in time order, not just the saved versions."""
    await _load(ctx.workspace_id, project_id)
    db = get_db()
    versions = await _versions().find({"project_id": project_id, "workspace_id": ctx.workspace_id}, {"edit": 0}).sort("created_at", -1).to_list(300)
    renders = await db["video_jobs"].find({"workspace_id": ctx.workspace_id, "project_id": project_id}).sort("created_at", -1).to_list(60)
    decisions = await db["video_review_decisions"].find({"workspace_id": ctx.workspace_id, "project_id": project_id}).sort("at", -1).to_list(60)
    return {"events": _history_rows(versions, renders, decisions)[:150]}


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
    opened = await _replace_edit(ctx, doc, edl.validate(old["edit"]), "restore", "Restored")
    from app.video import activity

    await activity.version_restored(ctx.workspace_id, ctx.user_id, project_id, version, int(opened.get("version") or version))
    return opened


@router.post("/{project_id}/reset")
@limiter.limit("30/minute")
async def reset_project(request: Request, project_id: str, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict:
    doc = await _load(ctx.workspace_id, project_id)
    opened = await _replace_edit(ctx, doc, edl.validate(doc.get("initial_edit") or doc["edit"]), "reset", "Started again")
    from app.video import activity

    await activity.started_again(ctx.workspace_id, ctx.user_id, project_id, int(opened.get("version") or 0))
    return opened


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
    #: h265 makes a smaller file that some older players and editors cannot open
    codec: Literal["h264", "h265"] = "h264"


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
        compiled = edl.to_job(doc["edit"], media, {**PRESETS[body.preset], "lufs": body.lufs, **({"codec": "h265"} if body.codec == "h265" else {})})
    except edl.EditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        job = await jobs.enqueue(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, inputs=compiled["inputs"], edit=compiled["edit"], output=compiled["output"],
            label=f"Rendering {doc.get('title') or 'video'}", project_id=project_id, show_in_activity=True,
            href=f"/dashboard/pipelines/video?project={project_id}", origin=summarise(m["origin"] for m in media.values()),
            idem_key=f"proj:{project_id}:v{doc['version']}:{body.preset}:{body.lufs}:{body.codec}",
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


HANDOFF_FORMATS = {
    "premiere": ("timeline_premiere.xml", "application/xml"),
    "finalcut": ("timeline_finalcut.fcpxml", "application/xml"),
    "edl": ("timeline.edl", "text/plain"),
    "otio": ("timeline.otio", "application/json"),
    "markers": ("markers.csv", "text/csv"),
    "srt": ("captions.srt", "application/x-subrip"),
    "vtt": ("captions.vtt", "text/vtt"),
    "package": ("handoff.zip", "application/zip"),
}


@router.get("/{project_id}/handoff/{fmt}")
@limiter.limit("30/minute")
async def handoff(request: Request, project_id: str, fmt: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> Response:
    """The project for an editing program: a timeline in Premiere (also Resolve), Final Cut or EDL form, the markers, the captions as
    SRT or VTT, or all of it in one package. Looks, crops, text and shapes stay in the editor; the package says so."""
    from app.video.handoff import formats, package
    from app.video.handoff.timeline import build

    doc = await _load(ctx.workspace_id, project_id) if fmt in HANDOFF_FORMATS else None
    return await handoff_response(ctx.workspace_id, doc, fmt)


async def handoff_response(workspace_id: str, doc: Optional[dict], fmt: str) -> Response:
    """The file for one hand-off format of a loaded project, or the plain refusal. Used by the member route and the public API."""
    from app.video.handoff import formats, package
    from app.video.handoff.timeline import build

    if fmt not in HANDOFF_FORMATS or doc is None:
        raise HTTPException(status_code=404, detail="That is not a format this project can be sent in.")
    edit = doc["edit"]
    tl = build(doc.get("title") or "Untitled video", edit, await _media_map(workspace_id, edl.media_ids(edit)))
    words = [{"w": w["w"], "s_ms": w["s_ms"], "e_ms": w["e_ms"]} for w in (edit.get("captions") or {}).get("words", [])]
    if fmt in ("srt", "vtt") and not words:
        raise HTTPException(status_code=409, detail="This video has no captions yet, so there is no caption file to send.")
    if not tl.events and fmt != "markers":
        raise HTTPException(status_code=409, detail="This project has no clips yet, so there is nothing to send.")
    if fmt == "package":
        body: bytes | str = package.build_zip(tl, words, " ".join(w["w"] for w in words))
    else:
        from app.video.handoff import importer

        body = {"premiere": formats.to_fcp7_xml, "finalcut": formats.to_fcpxml, "edl": formats.to_cmx3600, "markers": formats.to_markers_csv,
                "otio": importer.to_otio}.get(fmt, lambda t: formats.to_srt(words) if fmt == "srt" else formats.to_vtt(words))(tl)
    name, mime = HANDOFF_FORMATS[fmt]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (doc.get("title") or "video"))[:40] or "video"
    return Response(content=body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{safe}_{name}"', "Cache-Control": "no-store"})


MAX_IMPORT_BYTES = 5 * 1024 * 1024


@router.post("/{project_id}/import-timeline")
@limiter.limit("20/minute")
async def import_timeline(
    request: Request, project_id: str, file: UploadFile = File(...), apply: bool = Query(False),
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Read a timeline made in another program (an EDL, Final Cut Pro 7 XML or OpenTimelineIO file) and match its clips to this library by
    original file name. Without `apply` it only reports what would come in. With it, the project's video clips, audio clips and markers are
    replaced as one new version the member can go back from; captions, text and shapes stay as they were."""
    from app.video.handoff import importer
    from app.video.handoff.timeline import timecode

    doc = await _load(ctx.workspace_id, project_id)
    data = await file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="This file is too large to read as a timeline (the limit is 5 MB).")
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="This file is not a text timeline. Export an EDL, Final Cut Pro 7 XML or OpenTimelineIO file.")
    fps = doc["edit"].get("fps") or {"num": 30, "den": 1}
    try:
        tl = importer.parse(file.filename or "", text, max(1, int(round(fps["num"] / fps["den"]))))
    except importer.ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    rows = await media_assets.find({"workspace_id": ctx.workspace_id, "kind": {"$in": ["video", "audio"]}}, {"id": 1, "url": 1}).to_list(length=2000)
    library = {r["id"]: {"id": r["id"], "url": r.get("url")} for r in rows}
    matched, missing = importer.match_clips(tl, library)
    summary = {
        "title": tl.title, "clips": len(tl.events), "matched": len(matched), "unmatched": missing, "markers": len(tl.markers),
        "length": timecode(tl.duration_f, tl.timebase), "applied": False,
    }
    if tl.timebase != max(1, int(round(fps["num"] / fps["den"]))):
        summary["note"] = f"This timeline runs at {tl.timebase} frames a second and the project at {round(fps['num'] / fps['den'])}. Times are kept as frame counts."
    if not apply:
        return summary
    parts = importer.to_edit_parts(tl, matched)
    if not parts["video"] and not parts["audio"]:
        raise HTTPException(status_code=409, detail="None of the clips in this file match a file in your library by name, so nothing was changed. Upload the originals first.")
    edit = {**doc["edit"], "video": parts["video"], "audio": parts["audio"], "markers": parts["markers"] or doc["edit"].get("markers", []),
            "duration_f": max(parts["duration_f"], 1)}
    try:
        validated = edl.validate(edit)
        await _check_media(ctx.workspace_id, validated)
    except edl.EditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    opened = await _replace_edit(ctx, doc, validated, "import", "Imported a timeline")
    return {**summary, "applied": True, "version": opened.get("version")}


@router.get("/{project_id}/renders")
@limiter.limit("120/minute")
async def list_renders(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    await _load(ctx.workspace_id, project_id)
    rows = await get_db()["video_jobs"].find({"workspace_id": ctx.workspace_id, "project_id": project_id}).sort("created_at", -1).to_list(20)
    return {"renders": [jobs.public(r) for r in rows]}
