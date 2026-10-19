"""Client review links for a finished video. A member shares a link; the client watches it without an account, leaves comments at a moment
of the video, and approves it or asks for changes. A link can also hold an earlier version for a side-by-side compare.

Decisions made by the owner (2026-10-15): a link is view only, lasts 14 days unless the member picks 7 to 30, a commenter types a name,
and the member can end a link at any time. An unknown, expired or ended link all answer the same plain 404 so nothing can be learned by
probing. The public side never carries a workspace, user or brand id."""


import hashlib
import hmac
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.middleware import limiter
from app.core.turnstile import verify_turnstile
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import get_db, media_assets

logger = logging.getLogger(__name__)
member_router = APIRouter()
public_router = APIRouter()

DEFAULT_DAYS = 14
MIN_DAYS, MAX_DAYS = 7, 30
MAX_COMMENTS_PER_LINK = 500
MAX_DECISIONS_PER_LINK = 100
MAX_LINKS_PER_PROJECT = 50
NOT_AVAILABLE = "This link isn't available. It may have expired or been ended."


def _links():
    return get_db()["video_review_links"]


def _comments():
    return get_db()["video_review_comments"]


def _decisions():
    return get_db()["video_review_decisions"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return None


def _iso(v: Any) -> Optional[str]:
    d = _aware(v)
    return d.isoformat().replace("+00:00", "Z") if d else None


def _live(link: Optional[dict]) -> bool:
    expires = _aware((link or {}).get("expires_at"))
    return bool(link) and not link.get("revoked") and bool(expires) and expires > _now()


# ── the member's side ────────────────────────────────────────────────────────────

class CreateLinkBody(BaseModel):
    days: int = Field(DEFAULT_DAYS, ge=MIN_DAYS, le=MAX_DAYS)
    #: an earlier finished render of the same project, shown beside the current one
    compare_job_id: Optional[str] = None
    note: str = Field("", max_length=300)


async def _render_media(workspace_id: str, project_id: str, job_id: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
    """(job, media) of a finished render of this project. `job_id` None means the project's latest finished one."""
    flt: dict[str, Any] = {"workspace_id": workspace_id, "project_id": project_id, "status": "done", "media_id": {"$ne": None}}
    if job_id:
        flt["id"] = job_id
    job = await get_db()["video_jobs"].find_one(flt, sort=[("finished_at", -1)])
    if not job:
        return None, None
    media = await media_assets.find_one({"id": job["media_id"], "workspace_id": workspace_id})
    return job, media


def _item(link: dict, comments: int, open_comments: int, decision: Optional[dict]) -> dict[str, Any]:
    return {
        "id": link["id"], "url": f"{settings.FRONTEND_URL}/review/{link['token']}", "created_at": _iso(link.get("created_at")),
        "expires_at": _iso(link.get("expires_at")), "active": _live(link), "ended": bool(link.get("revoked")),
        "has_compare": bool(link.get("compare_media_id")), "note": link.get("note", ""), "comments": comments, "open_comments": open_comments,
        "decision": ({"outcome": decision["outcome"], "name": decision.get("name"), "note": decision.get("note"), "at": _iso(decision.get("at"))}
                     if decision else None),
    }


@member_router.post("/projects/{project_id}/links", status_code=201)
@limiter.limit("10/minute")
async def create_link(request: Request, project_id: str, body: CreateLinkBody, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict[str, Any]:
    project = await get_db()["video_projects"].find_one({"id": project_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}})
    if not project:
        raise HTTPException(status_code=404, detail="Video project not found.")
    job, media = await _render_media(ctx.workspace_id, project_id, None)
    if not job or not media:
        raise HTTPException(status_code=409, detail="Make the video first. A review link shows the finished video.")
    compare_media = None
    if body.compare_job_id:
        other, compare_media = await _render_media(ctx.workspace_id, project_id, body.compare_job_id)
        if not other or not compare_media:
            raise HTTPException(status_code=404, detail="That earlier version was not found.")
        if other["id"] == job["id"]:
            raise HTTPException(status_code=400, detail="Pick an earlier version to compare with, not the current one.")
    if await _links().count_documents({"workspace_id": ctx.workspace_id, "project_id": project_id, "revoked": {"$ne": True}, "expires_at": {"$gt": _now()}}) >= MAX_LINKS_PER_PROJECT:
        raise HTTPException(status_code=429, detail="This video already has as many open review links as it can hold. End one you no longer need first.")
    now = _now()
    link = {
        "id": uuid4().hex, "token": secrets.token_urlsafe(24), "workspace_id": ctx.workspace_id, "project_id": project_id, "title": project.get("title") or "Video",
        "job_id": job["id"], "media_id": media["id"], "compare_job_id": body.compare_job_id, "compare_media_id": (compare_media or {}).get("id"),
        "created_by": ctx.user_id, "created_at": now, "expires_at": now + timedelta(days=body.days), "revoked": False, "note": body.note.strip(),
    }
    await _links().insert_one(dict(link))
    return _item(link, 0, 0, None)


@member_router.get("/projects/{project_id}/links")
@limiter.limit("60/minute")
async def list_links(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    rows = await _links().find({"workspace_id": ctx.workspace_id, "project_id": project_id}).sort("created_at", -1).to_list(length=50)
    out = []
    for link in rows:
        total = await _comments().count_documents({"link_id": link["id"]})
        open_n = await _comments().count_documents({"link_id": link["id"], "resolved": {"$ne": True}})
        decision = await _decisions().find_one({"link_id": link["id"]}, sort=[("at", -1)])
        out.append(_item(link, total, open_n, decision))
    return {"links": out}


@member_router.delete("/links/{link_id}", status_code=204)
@limiter.limit("30/minute")
async def end_link(request: Request, link_id: str, ctx: WorkspaceContext = Depends(require("create_content"))) -> Response:
    res = await _links().update_one({"id": link_id, "workspace_id": ctx.workspace_id}, {"$set": {"revoked": True, "revoked_at": _now()}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="That link was not found.")
    return Response(status_code=204)


def _comment_view(c: dict) -> dict[str, Any]:
    return {"id": c["id"], "time_ms": c["time_ms"], "text": c["text"], "author_name": c["author_name"], "is_guest": c.get("is_guest", True),
            "created_at": _iso(c.get("created_at")), "resolved": bool(c.get("resolved")), "replies": c.get("replies") or []}


@member_router.get("/projects/{project_id}/comments")
@limiter.limit("60/minute")
async def project_comments(request: Request, project_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """Every comment on every link of this project, in video order, for the editor's review panel."""
    rows = await _comments().find({"workspace_id": ctx.workspace_id, "project_id": project_id}).sort("time_ms", 1).to_list(length=MAX_COMMENTS_PER_LINK * 4)
    return {"comments": [{**_comment_view(c), "link_id": c["link_id"]} for c in rows]}


class ResolveBody(BaseModel):
    resolved: bool = True


@member_router.put("/comments/{comment_id}/resolved")
@limiter.limit("60/minute")
async def resolve_comment(request: Request, comment_id: str, body: ResolveBody, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    res = await _comments().find_one_and_update({"id": comment_id, "workspace_id": ctx.workspace_id}, {"$set": {"resolved": body.resolved}}, return_document=True)
    if not res:
        raise HTTPException(status_code=404, detail="That comment was not found.")
    return _comment_view(res)


class ReplyBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


@member_router.post("/comments/{comment_id}/replies", status_code=201)
@limiter.limit("30/minute")
async def reply_to_comment(request: Request, comment_id: str, body: ReplyBody, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    reply = {"id": uuid4().hex, "text": body.text.strip(), "author_name": ctx.user.get("name") or "The team", "created_at": _iso(_now())}
    res = await _comments().find_one_and_update({"id": comment_id, "workspace_id": ctx.workspace_id}, {"$push": {"replies": reply}}, return_document=True)
    if not res:
        raise HTTPException(status_code=404, detail="That comment was not found.")
    return _comment_view(res)


# ── the client's side: no account ────────────────────────────────────────────────

class PublicMedia(BaseModel):
    url: str
    mime_type: str = "video/mp4"
    duration_s: Optional[float] = None
    poster_url: Optional[str] = None


class PublicReview(BaseModel):
    contains_ai: bool = False
    title: str
    expires_at: str
    media: PublicMedia
    compare: Optional[PublicMedia] = None
    note: str = ""
    decision: Optional[dict[str, Any]] = None


async def _live_link_or_404(token: str) -> dict:
    link = await _links().find_one({"token": token})
    if not _live(link):
        raise HTTPException(status_code=404, detail=NOT_AVAILABLE)
    return link


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


async def _public_media(media_id: Optional[str], workspace_id: str) -> Optional[PublicMedia]:
    if not media_id:
        return None
    m = await media_assets.find_one({"id": media_id, "workspace_id": workspace_id})
    return PublicMedia(url=m["url"], mime_type=m.get("mime_type") or "video/mp4", duration_s=m.get("duration_s"), poster_url=m.get("poster_url")) if m else None


@public_router.get("/{token}", response_model=PublicReview)
@limiter.limit("60/minute")
async def open_review(request: Request, token: str) -> PublicReview:
    link = await _live_link_or_404(token)
    media = await _public_media(link["media_id"], link["workspace_id"])
    if not media:
        raise HTTPException(status_code=404, detail=NOT_AVAILABLE)
    decision = await _decisions().find_one({"link_id": link["id"]}, sort=[("at", -1)])
    stored = await media_assets.find_one({"id": link["media_id"], "workspace_id": link["workspace_id"]}, {"contains_ai": 1, "origin": 1})
    return PublicReview(
        contains_ai=bool((stored or {}).get("contains_ai")) or (stored or {}).get("origin") == "ai",
        title=link.get("title") or "Video", expires_at=_iso(link["expires_at"]) or "", media=media,
        compare=await _public_media(link.get("compare_media_id"), link["workspace_id"]), note=link.get("note", ""),
        decision={"outcome": decision["outcome"], "name": decision.get("name"), "at": _iso(decision.get("at"))} if decision else None,
    )


@public_router.get("/{token}/comments")
@limiter.limit("60/minute")
async def public_comments(request: Request, token: str) -> list[dict[str, Any]]:
    link = await _live_link_or_404(token)
    rows = await _comments().find({"link_id": link["id"]}).sort("time_ms", 1).to_list(length=MAX_COMMENTS_PER_LINK)
    return [{"time_ms": c["time_ms"], "text": c["text"], "author_name": c["author_name"], "created_at": _iso(c.get("created_at")),
             "resolved": bool(c.get("resolved")), "replies": c.get("replies") or []} for c in rows]


class GuestComment(BaseModel):
    time_ms: int = Field(..., ge=0, le=6 * 3600 * 1000)
    text: str = Field(..., max_length=1000)
    guest_name: str = Field("", max_length=80)
    website: str = ""            # a field real visitors never see; any value means a bot
    turnstile_token: str = ""


@public_router.post("/{token}/comments", status_code=201)
@limiter.limit("5/minute")
async def add_comment(request: Request, token: str, body: GuestComment) -> dict[str, Any]:
    link = await _live_link_or_404(token)
    text, name = body.text.strip(), body.guest_name.strip()[:80]
    if not text:
        raise HTTPException(status_code=400, detail="Write something first.")
    if not name:
        raise HTTPException(status_code=400, detail="Add your name so the team knows who wrote this.")
    now = _now()
    if body.website.strip():
        return {"time_ms": body.time_ms, "text": text, "author_name": name, "created_at": _iso(now), "resolved": False, "replies": []}   # a bot is told it worked
    if not await verify_turnstile(body.turnstile_token, _client_ip(request)):
        raise HTTPException(status_code=400, detail="That didn't verify. Reload the page and try again.")
    if await _comments().count_documents({"link_id": link["id"]}) >= MAX_COMMENTS_PER_LINK:
        raise HTTPException(status_code=429, detail="This review has as many comments as it can hold.")
    doc = {"id": uuid4().hex, "link_id": link["id"], "workspace_id": link["workspace_id"], "project_id": link["project_id"], "time_ms": body.time_ms,
           "text": text, "author_name": name, "is_guest": True, "created_at": now, "resolved": False, "replies": []}
    await _comments().insert_one(dict(doc))
    return {"time_ms": body.time_ms, "text": text, "author_name": name, "created_at": _iso(now), "resolved": False, "replies": []}


class ReportBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)
    contact: str = Field("", max_length=200)
    website: str = ""


@public_router.post("/{token}/report", status_code=201)
@limiter.limit("5/minute")
async def report(request: Request, token: str, body: ReportBody) -> dict[str, Any]:
    """Anyone with a review link can report the video: the link is switched off at once and staff look at the report. This is the
    takedown path for content that should not be shared."""
    link = await _live_link_or_404(token)
    if body.website.strip():
        return {"received": True}
    await _links().update_one({"id": link["id"]}, {"$set": {"revoked": True, "revoked_at": _now(), "held_for_report": True}})
    await get_db()["video_takedown_requests"].insert_one({
        "id": uuid4().hex, "workspace_id": link["workspace_id"], "project_id": link["project_id"], "media_id": link["media_id"], "link_id": link["id"],
        "reason": body.reason.strip(), "contact": body.contact.strip() or None, "status": "open", "at": _now()})
    return {"received": True}


class DecisionBody(BaseModel):
    outcome: Literal["approved", "changes_requested"]
    name: str = Field(..., min_length=1, max_length=80)
    note: str = Field("", max_length=500)
    website: str = ""
    turnstile_token: str = ""


@public_router.post("/{token}/decision", status_code=201)
@limiter.limit("5/minute")
async def decide(request: Request, token: str, body: DecisionBody) -> dict[str, Any]:
    """The client's answer: approved, or changes wanted. The latest answer is the one shown; earlier ones are kept."""
    link = await _live_link_or_404(token)
    if body.website.strip():
        return {"outcome": body.outcome, "name": body.name.strip()}
    if not await verify_turnstile(body.turnstile_token, _client_ip(request)):
        raise HTTPException(status_code=400, detail="That didn't verify. Reload the page and try again.")
    if await _decisions().count_documents({"link_id": link["id"]}) >= MAX_DECISIONS_PER_LINK:
        raise HTTPException(status_code=429, detail="This review has had as many answers as it can hold.")
    await _decisions().insert_one({"id": uuid4().hex, "link_id": link["id"], "workspace_id": link["workspace_id"], "project_id": link["project_id"],
                                   "outcome": body.outcome, "name": body.name.strip()[:80], "note": body.note.strip(), "at": _now()})
    from app.video import activity

    await activity.review_answered(link["workspace_id"], link["created_by"], link["project_id"], link["id"], body.outcome, body.name.strip()[:80], body.note.strip())
    return {"outcome": body.outcome, "name": body.name.strip()}
