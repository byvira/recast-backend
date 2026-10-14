"""Stock footage for a video: search the connected libraries, a rail of the lines worth showing footage for, ranked options for a line,
and using a clip (saved to the workspace with its licence and author) or placing it in a project as a cutaway."""
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import brand_profiles, get_db, media_assets
from app.models.media import MediaSource
from app.video import transcribe
from app.video.stock import providers, rank
from app.video.stock import service as S

logger = logging.getLogger(__name__)


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])

Shape = Literal["wide", "tall", "square"]


def _public(c: dict[str, Any]) -> dict[str, Any]:
    keep = ("provider", "id", "page_url", "preview_url", "poster_url", "width", "height", "duration_s", "author", "author_url", "license",
            "license_url", "tags", "score", "reasons", "shape")
    return {k: c.get(k) for k in keep}


@router.get("/status")
@limiter.limit("120/minute")
async def status(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    connected = providers.configured()
    return {"connected": connected, "ready": bool(connected),
            "note": None if connected else "No stock library is connected yet, so footage suggestions are not available. Your own pictures still work."}


@router.get("/search")
@limiter.limit("60/minute")
async def search(
    request: Request, q: str = Query(..., min_length=2, max_length=100), shape: Shape = "wide", min_seconds: float = Query(3.0, ge=0, le=120),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    found = await providers.search(q)
    ranked = rank.rank(found["clips"], q, want=shape, min_seconds=min_seconds, query=q)
    return {"clips": [_public(c) for c in ranked[:24]], "cached": found["cached"], "errors": found["errors"],
            "connected": bool(providers.configured())}


async def _words(ctx: WorkspaceContext, media_id: str) -> tuple[dict, list[dict]]:
    media = await media_assets.find_one({"id": media_id, "workspace_id": ctx.workspace_id})
    if not media:
        raise HTTPException(status_code=404, detail="That video is not in this workspace's library.")
    tr = await transcribe._transcripts().find_one({"workspace_id": ctx.workspace_id, "media_id": media_id})
    if not tr or tr["status"] not in ("done", "partial"):
        raise HTTPException(status_code=409, detail="Write out what was said in this video first. Footage is matched to what is said.")
    return tr, await transcribe.all_words(tr["id"])


@router.get("/rail/{media_id}")
@limiter.limit("60/minute")
async def keyword_rail(
    request: Request, media_id: str, limit: int = Query(12, ge=1, le=30), ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """The lines of the video worth footage, each with a search query made from its most telling words. No model is called."""
    tr, words = await _words(ctx, media_id)
    beats = rank.rail(words, limit=limit)
    return {"beats": beats, "language": tr.get("language"), "needs_translation": (tr.get("language") or "en").split("-")[0].lower() != "en"}


class OptionsBody(BaseModel):
    media_id: str
    indexes: list[int] = Field(..., min_length=1, max_length=6)
    shape: Shape = "wide"
    improve_queries: bool = False


@router.post("/rail/options")
@limiter.limit("20/minute")
async def rail_options(request: Request, body: OptionsBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Ranked stock options for chosen lines of the rail. For a video not in English (or when asked), the model writes the English
    search queries first. A line with no stock result gets the brand's own pictures as the fallback."""
    tr, words = await _words(ctx, body.media_id)
    beats = rank.rail(words, limit=30)
    chosen = [b for b in beats if b["index"] in set(body.indexes)]
    if len(chosen) != len(set(body.indexes)):
        raise HTTPException(status_code=404, detail="One of those lines is not on the rail.")
    foreign = (tr.get("language") or "en").split("-")[0].lower() != "en"
    voice = None
    out = []
    brand_stills: Optional[list[dict]] = None
    for beat in chosen:
        queries = [beat["query"]]
        if foreign or body.improve_queries:
            from app.agents.supervisor.service import assert_ai_budget_available
            from app.video.create import generate as G
            from app.video.create import voice as voice_mod

            try:
                await assert_ai_budget_available(ctx.workspace_id)
                voice = voice or await voice_mod.load(ctx.workspace_id, None)
                got, _ = await G.broll(beat["line"], "", voice=voice)
                queries = got.get("queries") or queries
            except HTTPException:
                pass
        clips: list[dict] = []
        errors: list[str] = []
        used = None
        for q in queries[:3]:
            res = await providers.search(q)
            errors.extend(res["errors"])
            if res["clips"]:
                clips = rank.rank(res["clips"], beat["line"], want=body.shape, min_seconds=min(6.0, max(2.0, beat["seconds"])), query=q)
                used = q
                break
        fallback = None
        if not clips:
            if brand_stills is None:
                rows = await media_assets.find({"workspace_id": ctx.workspace_id, "source": MediaSource.BRAND_ASSET.value, "kind": "image"}).limit(6).to_list(6)
                brand_stills = [{"media_id": r["id"], "url": r["url"], "kind": "image"} for r in rows]
            fallback = brand_stills
        out.append({"index": beat["index"], "line": beat["line"], "queries": queries, "query_used": used, "options": [_public(c) for c in clips[:6]],
                    "fallback_pictures": fallback, "errors": sorted(set(errors))})
    return {"beats": out, "connected": bool(providers.configured())}


class UseBody(BaseModel):
    provider: Literal["pixabay", "pexels"]
    clip_id: str = Field(..., min_length=1, max_length=40)
    query: str = Field("", max_length=100)
    project_id: Optional[str] = None
    at_ms: int = Field(0, ge=0)
    duration_ms: Optional[int] = Field(None, ge=500, le=600000)


@router.post("/use", status_code=201)
@limiter.limit("30/minute")
async def use(request: Request, body: UseBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    """Save a stock clip to the workspace with its licence and author, and (with a project) place it as a cutaway at a time."""
    media = await S.use_clip(workspace_id=ctx.workspace_id, user_id=ctx.user_id, provider=body.provider, clip_id=body.clip_id, query=body.query)
    result: dict[str, Any] = {"media": {"id": media.id, "url": media.url, "duration_s": media.duration_s, "origin": media.origin,
                                        "contains_stock": media.contains_stock, "stock": media.stock, "credit": S.credit_line(media.model_dump())}}
    if body.project_id:
        from app.api.v1.video_projects import _load, _open, _replace_edit

        doc = await _load(ctx.workspace_id, body.project_id)
        edit, clip = S.insert_clip(doc["edit"], media.model_dump(), at_ms=body.at_ms, duration_ms=body.duration_ms)
        project = await _replace_edit(ctx, doc, edit, "stock", "Added stock footage")
        result["project"] = project
        result["clip"] = clip
    return result


@router.get("/credits/{project_id}")
@limiter.limit("60/minute")
async def credits(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """The credits for every stock clip a video was made from, ready to paste into a description."""
    from app.video import edl

    project = await get_db()["video_projects"].find_one({"workspace_id": ctx.workspace_id, "id": project_id, "deleted": {"$ne": True}})
    if not project:
        raise HTTPException(status_code=404, detail="That project was not found.")
    ids = edl.media_ids(project["edit"])
    rows = await media_assets.find({"workspace_id": ctx.workspace_id, "id": {"$in": list(ids)}, "source": MediaSource.STOCK.value}).to_list(200)
    items = [{"media_id": r["id"], "credit": S.credit_line(r), "page_url": (r.get("stock") or {}).get("page_url"), "license": (r.get("stock") or {}).get("license")}
             for r in rows]
    return {"credits": items, "text": "\n".join(i["credit"] for i in items if i["credit"])}
