"""Covers and thumbnails: pictures of a video's best moments, ranked, cut to each platform's size and built as editable pictures.

Candidate frames are taken by the render worker from the video's preview copy and measured there. Ranking is done here from the
measurements, so a different platform or brand colour re-ranks instantly with no new work. A cover is an ordinary picture
project: the Image editor opens it.
"""
import logging
from datetime import timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import brand_profiles, get_db, media_assets
from app.video import jobs
from app.video.covers import compose, rank, text as cover_text
from app.video.covers.platforms import PLATFORMS, public_table
from app.video.origin import origin_of

logger = logging.getLogger(__name__)


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])


def _platform(name: str) -> str:
    if name not in PLATFORMS:
        raise HTTPException(status_code=400, detail="That platform is not one covers are made for.")
    return name


async def _video(ctx: WorkspaceContext, media_id: str) -> dict:
    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    kind = (media or {}).get("kind")
    kind = kind.value if hasattr(kind, "value") else kind
    if not media or kind != "video":
        raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
    return media


async def _brand_id(ctx: WorkspaceContext, brand_id: Optional[str]) -> str:
    if brand_id:
        if not await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id}, {"id": 1}):
            raise HTTPException(status_code=404, detail="Brand profile not found.")
        return brand_id
    first = await brand_profiles.find_one({"workspace_id": ctx.workspace_id}, {"id": 1}, sort=[("created_at", 1)])
    if not first:
        raise HTTPException(status_code=409, detail="Add a brand first. A cover uses the brand's colours, fonts and logo.")
    return first["id"]


async def _brand_hex(brand_id: str, ctx: WorkspaceContext) -> Optional[str]:
    brand = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id}, {"visual_identity": 1})
    return (((brand or {}).get("visual_identity") or {}).get("colors") or {}).get("primary")


def _text_room(platform_id: str) -> str:
    return "bottom"


async def _frames_doc(ctx: WorkspaceContext, media_id: str) -> Optional[dict]:
    return await get_db()["video_cover_frames"].find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})


async def _latest_job(ctx: WorkspaceContext, media_id: str) -> Optional[dict]:
    return await get_db()["video_jobs"].find_one(
        {"workspace_id": ctx.workspace_id, "kind": "frames", "source_media_id": media_id}, sort=[("created_at", -1)],
    )


def _view(f: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "t", "url", "score", "reasons", "notes", "duplicate_of", "face_count")
    return {**{k: f.get(k) for k in keep}, "t_s": f["t"]}


@router.get("/platforms")
@limiter.limit("120/minute")
async def platforms(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return {"platforms": public_table()}


@router.get("/{media_id}")
@limiter.limit("240/minute")
async def state(
    request: Request, media_id: str, platform: str = Query("youtube"), brand_id: Optional[str] = None,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    media = await _video(ctx, media_id)
    platform = _platform(platform)
    doc = await _frames_doc(ctx, media_id)
    job = await _latest_job(ctx, media_id)
    base = {"media_id": media_id, "platform": platform, "preview_status": media.get("preview_status") or "none"}
    if doc and doc.get("frames"):
        brand_hex = await _brand_hex(brand_id, ctx) if brand_id else None
        ranked = rank.rank_frames(doc["frames"], text_room=_text_room(platform), brand_hex=brand_hex, faces_checked=bool(doc.get("faces_checked")))
        note = None if doc.get("faces_checked") else "Faces could not be checked on this setup, so pictures are chosen by sharpness, light and room for words."
        return {**base, "status": "done", "frames": [_view(f) for f in ranked], "faces_checked": bool(doc.get("faces_checked")), "note": note,
                "stale": bool(job and job.get("status") in ("queued", "claimed"))}
    if job and job.get("status") in ("queued", "claimed"):
        return {**base, "status": "running" if job["status"] == "claimed" else "queued", "frames": [], "faces_checked": False, "note": None}
    if job and job.get("status") in ("failed", "blocked"):
        return {**base, "status": "failed", "frames": [], "faces_checked": False, "note": job.get("error") or "The pictures could not be made."}
    return {**base, "status": "none", "frames": [], "faces_checked": False, "note": None}


@router.post("/{media_id}/frames", status_code=202)
@limiter.limit("20/minute")
async def start_frames(request: Request, media_id: str, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    media = await _video(ctx, media_id)
    preview = media.get("preview") or {}
    if media.get("preview_status") != "ready" or not preview.get("proxy_url"):
        raise HTTPException(status_code=409, detail="This video's preview is still being prepared. Cover pictures are chosen from it.")
    meta = preview.get("meta") or {}
    duration = float(media.get("duration_s") or meta.get("duration_s") or 0)
    analysis = await get_db()["video_analysis"].find_one({"workspace_id": ctx.workspace_id, "media_id": media_id}, {"moments": 1})
    times = rank.candidate_times(duration, meta.get("scenes") or [], (analysis or {}).get("moments") or [])
    if not times:
        raise HTTPException(status_code=409, detail="This video is too short to choose cover pictures from.")
    try:
        job = await jobs.enqueue_frames(workspace_id=ctx.workspace_id, user_id=ctx.user_id, media_id=media_id, url=preview["proxy_url"], times_s=times)
    except jobs.JobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return {"job": {"id": job["id"], "status": job["status"]}, "times": len(times), "worker_ready": await jobs.worker_ready()}


class CoversBody(BaseModel):
    platform: str
    brand_id: Optional[str] = None
    count: int = Field(3, ge=1, le=3)
    frame_ids: Optional[list[str]] = Field(None, max_length=3)
    text: Optional[str] = Field(None, max_length=120)
    language: str = Field("en", max_length=10)


async def _fetch(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            res = await client.get(url)
        return res.content if res.status_code == 200 and 0 < len(res.content) <= 12 * 1024 * 1024 else None
    except httpx.HTTPError as exc:
        logger.warning("Could not read a cover frame: %s", exc)
        return None


async def _material(ctx: WorkspaceContext, media_id: str, media: dict) -> tuple[str, str]:
    """(title, material) the cover words are written from: the reading of the video when there is one."""
    analysis = await get_db()["video_analysis"].find_one({"workspace_id": ctx.workspace_id, "media_id": media_id}, {"outline": 1})
    outline = (analysis or {}).get("outline") or {}
    title = outline.get("title") or media.get("title") or media.get("filename") or ""
    parts = [outline.get("title"), outline.get("summary"), ", ".join(outline.get("topics") or [])]
    material = "\n".join(p for p in parts if p)
    if not material:
        # not read yet: the opening of what was said is enough to write words from
        tr = await get_db()["video_transcripts"].find_one(
            {"workspace_id": ctx.workspace_id, "media_id": media_id, "status": {"$in": ["done", "partial"]}})
        page = await get_db()["video_transcript_pages"].find_one({"transcript_id": tr["id"], "page": 0}) if tr else None
        material = " ".join(w.get("w", "") for w in (page or {}).get("words", [])[:350]).strip()
    return str(title)[:120], material


@router.post("/{media_id}/covers", status_code=201)
@limiter.limit("20/minute")
async def make_covers(
    request: Request, media_id: str, body: CoversBody, ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict[str, Any]:
    """Up to three cover options for one platform, each from a different moment, cut to the platform's size and saved as an
    editable picture."""
    media = await _video(ctx, media_id)
    platform_id = _platform(body.platform)
    doc = await _frames_doc(ctx, media_id)
    if not doc or not doc.get("frames"):
        raise HTTPException(status_code=409, detail="Choose the cover pictures for this video first.")
    brand_id = await _brand_id(ctx, body.brand_id)
    ranked = rank.rank_frames(doc["frames"], text_room=_text_room(platform_id), brand_hex=await _brand_hex(brand_id, ctx),
                              faces_checked=bool(doc.get("faces_checked")))
    if body.frame_ids:
        picks = [f for f in ranked if f["id"] in set(body.frame_ids)]
        if len(picks) != len(set(body.frame_ids)):
            raise HTTPException(status_code=404, detail="One of those pictures is not a candidate for this video.")
    else:
        picks = rank.pick_distinct(ranked, body.count)
    if not picks:
        raise HTTPException(status_code=409, detail="There is no usable picture in this video.")

    title, material = await _material(ctx, media_id, media)
    if body.text and body.text.strip():
        options, by_model = [{"text": cover_text.clean_line(body.text, PLATFORMS[platform_id]["max_chars"]), "accent": ""}], False
    else:
        from app.agents.supervisor.service import assert_ai_budget_available

        try:
            await assert_ai_budget_available(ctx.workspace_id)
            options, by_model = await cover_text.cover_options(platform_id=platform_id, material=material, title=title,
                                                               language=body.language, count=len(picks))
        except HTTPException:
            options, by_model = await cover_text.cover_options(platform_id=platform_id, material="", title=title, language=body.language)
    origin = {"origin": origin_of(media), "contains_ai": bool(media.get("contains_ai")), "contains_stock": bool(media.get("contains_stock"))}

    covers: list[dict[str, Any]] = []
    for n, frame in enumerate(picks):
        data = await _fetch(frame["url"])
        if not data:
            continue
        words = options[n % len(options)] if options else {"text": "", "accent": ""}
        made = await compose.make_cover(
            ctx, platform_id=platform_id, brand_id=brand_id, text=words["text"], accent=words.get("accent", ""), title=title,
            frame_bytes=data, faces=frame.get("faces"), origin=origin, link={"media_id": media_id, "frame_id": frame["id"], "t_s": frame["t"]},
        )
        covers.append({**made, "frame_id": frame["id"], "t_s": frame["t"], "score": frame.get("score"), "reasons": frame.get("reasons", []),
                       "words_by_model": by_model})
    if not covers:
        raise HTTPException(status_code=502, detail="The chosen pictures could not be read just now. Please try again.")
    return {"covers": covers, "platform": platform_id}


class ComposeBody(BaseModel):
    platform: str
    brand_id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=160)
    material: str = Field("", max_length=3000)
    picture_media_id: Optional[str] = None
    count: int = Field(3, ge=1, le=3)
    language: str = Field("en", max_length=10)


@router.post("/compose", status_code=201)
@limiter.limit("20/minute")
async def compose_cover(request: Request, body: ComposeBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Covers for things that are not a video: an audio episode, a text post or a carousel. With a picture from the library the
    picture is cut to the platform's shape; without one, the cover is the brand's colour, in up to three variations."""
    platform_id = _platform(body.platform)
    brand_id = await _brand_id(ctx, body.brand_id)
    picture: Optional[bytes] = None
    origin: Optional[dict[str, Any]] = None
    if body.picture_media_id:
        item = await media_assets.find_one({"id": body.picture_media_id, "workspace_id": ctx.workspace_id})
        kind = (item or {}).get("kind")
        kind = kind.value if hasattr(kind, "value") else kind
        if not item or kind != "image":
            raise HTTPException(status_code=404, detail="That picture is not in this workspace's library.")
        picture = await _fetch(item["url"])
        if not picture:
            raise HTTPException(status_code=502, detail="That picture could not be read just now. Please try again.")
        origin = {"origin": origin_of(item), "contains_ai": bool(item.get("contains_ai")), "contains_stock": bool(item.get("contains_stock"))}
    from app.agents.supervisor.service import assert_ai_budget_available

    try:
        await assert_ai_budget_available(ctx.workspace_id)
        options, by_model = await cover_text.cover_options(platform_id=platform_id, material=body.material, title=body.title,
                                                           language=body.language, count=body.count)
    except HTTPException:
        options, by_model = await cover_text.cover_options(platform_id=platform_id, material="", title=body.title, language=body.language)
    covers = []
    variations = ["primary", "secondary", "dark"]
    for n in range(1 if picture else body.count):
        words = options[n % len(options)] if options else {"text": body.title, "accent": ""}
        made = await compose.make_cover(
            ctx, platform_id=platform_id, brand_id=brand_id, text=words["text"], accent=words.get("accent", ""), title=body.title,
            frame_bytes=picture, faces=None if picture else None, backdrop=variations[n % 3], origin=origin,
            link={"picture_media_id": body.picture_media_id},
        )
        covers.append({**made, "words_by_model": by_model})
    return {"covers": covers, "platform": platform_id}
