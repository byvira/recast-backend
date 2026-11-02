"""The website reads the live copy of what Ops wrote: blog posts, open job posts and the pages edited from Ops. No sign-in, never a
draft. Anything set to go live at a time is put live here once its time has come."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.core.middleware import limiter
from app.site_content import rules as R
from app.site_content import store as C

router = APIRouter()
PAGE = 24
CACHE = "public, max-age=60"


def _post_card(doc: dict[str, Any]) -> dict[str, Any]:
    live = doc["live"]
    return {"slug": live["slug"], "title": live["title"], "summary": live["summary"], "author": live.get("author") or "",
            "cover_url": live.get("cover_url") or "", "cover_alt": live.get("cover_alt") or "", "published_at": doc.get("live_at"),
            "read_minutes": R.read_minutes(live.get("html") or "")}


def _job_card(doc: dict[str, Any]) -> dict[str, Any]:
    live = doc["live"]
    return {k: live.get(k) for k in ("slug", "title", "summary", "team", "place", "job_type", "pay", "closes_on")} | {
        "published_at": doc.get("live_at")}


@router.get("/posts")
@limiter.limit("120/minute")
async def posts(request: Request, response: Response, page: int = Query(0, ge=0, le=200)) -> dict:
    await C.promote_due()
    docs = await C.content_col().find({"kind": "post", "live": {"$ne": None}, "deleted": {"$ne": True}}, {"_id": 0, "draft": 0}) \
        .sort("live_at", -1).skip(page * PAGE).limit(PAGE).to_list(PAGE)
    response.headers["Cache-Control"] = CACHE
    return {"posts": [_post_card(d) for d in docs], "page": page, "more": len(docs) == PAGE}


@router.get("/posts/{slug}")
@limiter.limit("120/minute")
async def post(request: Request, response: Response, slug: str) -> dict:
    await C.promote_due()
    doc = await C.content_col().find_one({"kind": "post", "live.slug": slug[:80], "deleted": {"$ne": True}}, {"_id": 0, "draft": 0})
    if not doc or not doc.get("live"):
        raise HTTPException(status_code=404, detail="Not found.")
    response.headers["Cache-Control"] = CACHE
    live = doc["live"]
    return {"post": _post_card(doc) | {"html": live.get("html") or "", "seo_title": live.get("seo_title") or "",
                                       "seo_description": live.get("seo_description") or ""}}


@router.get("/jobs")
@limiter.limit("120/minute")
async def jobs(request: Request, response: Response) -> dict:
    await C.promote_due()
    docs = await C.content_col().find({"kind": "job", "live": {"$ne": None}, "deleted": {"$ne": True}}, {"_id": 0, "draft": 0}) \
        .sort("live_at", -1).limit(100).to_list(100)
    response.headers["Cache-Control"] = CACHE
    return {"jobs": [_job_card(d) for d in docs if R.is_open(d["live"])]}


@router.get("/jobs/{slug}")
@limiter.limit("120/minute")
async def job(request: Request, response: Response, slug: str) -> dict:
    await C.promote_due()
    doc = await C.content_col().find_one({"kind": "job", "live.slug": slug[:80], "deleted": {"$ne": True}}, {"_id": 0, "draft": 0})
    if not doc or not doc.get("live"):
        raise HTTPException(status_code=404, detail="Not found.")
    response.headers["Cache-Control"] = CACHE
    live = doc["live"]
    return {"job": _job_card(doc) | {"html": live.get("html") or "", "apply_email": live.get("apply_email") or "",
                                     "apply_url": live.get("apply_url") or "", "open": R.is_open(live)}}


@router.get("/pages/{key}")
@limiter.limit("120/minute")
async def page(request: Request, response: Response, key: str) -> dict:
    """A page's live words. 404 while nothing has been put live, so the website keeps its built-in words."""
    if key not in R.PAGES:
        raise HTTPException(status_code=404, detail="Not found.")
    await C.promote_due()
    doc = await C.page_doc(key)
    if not doc or doc.get("live") is None:
        raise HTTPException(status_code=404, detail="Not found.")
    response.headers["Cache-Control"] = CACHE
    return {"page": doc["live"], "published_at": doc.get("live_at")}
