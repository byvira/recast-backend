"""Content Guard routes.

Members: check a piece of text and get back the cleaned or rewritten version.
Ops (`/api/v1/ops/content-safety`): read and change the settings, see what was blocked or rewritten, and try a text against
the rules. Settings apply to every workspace, so changing them is for the Ops owner and reading them is for platform staff.
"""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.agents.content_guard import config as guard_config
from app.agents.content_guard.agent import review_text, rule_screen
from app.agents.content_guard.rules import CATEGORIES, ai_phrases_in, clean_text
from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.db.mongo import safety_events
from app.shared.activity import record_system

router = APIRouter()
ops_router = APIRouter()

MAX_TEXT = 8000


class CheckBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT)
    piece_id: Optional[str] = None
    platform: Optional[str] = None
    rewrite: bool = True


@router.post("/check")
@limiter.limit("20/minute")
async def check_text(request: Request, body: CheckBody, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    """The text with dashes and filler removed, rewritten when it was flagged and could be made safe, or marked blocked."""
    result = await review_text(
        body.text, where="check", workspace_id=ctx.workspace_id, piece_id=body.piece_id, platform=body.platform,
        allow_rewrite=body.rewrite,
    )
    return result.as_dict()


async def _ops_owner(user: dict = Depends(require_platform_staff)) -> dict:
    if not user.get("is_master_admin"):
        raise HTTPException(status_code=403, detail="Only the Ops owner can change content safety settings.")
    return user


def _public(settings_doc: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: settings_doc.get(k) for k in guard_config.DEFAULTS},
        "categories": [{"key": k, "label": v} for k, v in CATEGORIES.items()],
    }


class SettingsBody(BaseModel):
    version: int
    enabled: Optional[bool] = None
    strictness: Optional[Literal["standard", "strict"]] = None
    extra_blocked_terms: Optional[list[str]] = None
    allowed_terms: Optional[list[str]] = None
    ai_phrases: Optional[list[str]] = None
    rewrite_flagged: Optional[bool] = None
    media_check: Optional[bool] = None
    video_frames: Optional[bool] = None
    model_check: Optional[Literal["off", "risky", "publish"]] = None


@ops_router.get("")
@limiter.limit("30/minute")
async def get_settings(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return _public(await guard_config.load())


@ops_router.put("")
@limiter.limit("20/minute")
async def save_settings(request: Request, body: SettingsBody, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True, exclude={"version"})
    try:
        saved = await guard_config.save(changes, expected_version=body.version, user_id=str(user["id"]))
    except guard_config.VersionConflict:
        raise HTTPException(status_code=409, detail="These settings changed since you opened them. Reload and try again.")
    workspace_id = user.get("default_workspace_id")
    if workspace_id:
        try:
            await record_system(
                workspace_id=workspace_id,
                key=f"contentsafety:{uuid4()}",
                actor_name="Recast staff",
                actor_user_id=str(user["id"]),
                category="platform_ops",
                title="Content safety settings saved",
                description="Changed: " + ", ".join(sorted(changes)),
                visibility="admins",
                metadata={"event": "content_safety.saved", "changed": sorted(changes), "version": saved.get("version")},
            )
        except Exception:
            pass
    return _public(saved)


async def _staff_name(user_id: Optional[str]) -> Optional[str]:
    """The name of the staff member who reviewed an event, or None when there is none to show."""
    if not user_id:
        return None
    from app.db.mongo import users

    doc = await users.find_one({"id": user_id}, {"full_name": 1, "name": 1, "email": 1})
    return ((doc or {}).get("full_name") or (doc or {}).get("name") or (doc or {}).get("email")) or None


def _event_row(row: dict[str, Any]) -> dict[str, Any]:
    row["id"] = str(row.pop("_id"))
    row.pop("fingerprint", None)
    row["category_names"] = [CATEGORIES.get(c, c) for c in row.get("categories", [])]
    return row


@ops_router.get("/events")
@limiter.limit("30/minute")
async def list_events(
    request: Request,
    outcome: Optional[Literal["blocked", "rewritten"]] = None,
    category: Optional[str] = None,
    where: Optional[str] = None,
    platform: Optional[str] = None,
    workspace_id: Optional[str] = None,
    status: Optional[Literal["open", "reviewed"]] = None,
    since: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=10000),
    user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    flt: dict[str, Any] = {}
    for key, value in (("outcome", outcome), ("categories", category), ("where", where), ("platform", platform),
                       ("workspace_id", workspace_id)):
        if value:
            flt[key] = value
    if status == "reviewed":
        flt["status"] = "reviewed"
    elif status == "open":
        flt["status"] = {"$ne": "reviewed"}
    if since:
        flt["created_at"] = {"$gte": since}
    rows = [
        _event_row(row)
        async for row in safety_events.find(flt, {"fingerprint": 0}).sort("created_at", -1).skip(offset).limit(limit)
    ]
    return {"events": rows, "total": await safety_events.count_documents(flt)}


@ops_router.get("/events/{event_id}")
@limiter.limit("60/minute")
async def get_event(request: Request, event_id: str, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """One caught item with everything stored about it, plus the post as it is now (so a reviewer can see whether it was
    edited or published since), and the workspace and brand names."""
    from bson import ObjectId
    from bson.errors import InvalidId

    from app.db.mongo import brand_profiles, content_pieces, workspaces

    try:
        row = await safety_events.find_one({"_id": ObjectId(event_id)}, {"fingerprint": 0})
    except InvalidId:
        raise HTTPException(status_code=404, detail="That safety event was not found.")
    if not row:
        raise HTTPException(status_code=404, detail="That safety event was not found.")
    event = _event_row(row)

    workspace = await workspaces.find_one({"id": event.get("workspace_id")}, {"name": 1}) if event.get("workspace_id") else None
    brand = await brand_profiles.find_one({"id": event.get("brand_id")}, {"identity": 1}) if event.get("brand_id") else None
    event["workspace_name"] = (workspace or {}).get("name")
    event["reviewed_by_name"] = await _staff_name(event.get("reviewed_by"))
    event["brand_name"] = ((brand or {}).get("identity") or {}).get("name")

    piece = None
    if event.get("piece_id"):
        doc = await content_pieces.find_one(
            {"piece_id": event["piece_id"]},
            {"_id": 0, "piece_id": 1, "content": 1, "platform": 1, "approval_status": 1, "publish_status": 1,
             "flagged_for_review": 1, "quality_issues": 1, "updated_at": 1, "deleted": 1},
        )
        if doc:
            doc["content"] = (doc.get("content") or "")[:MAX_TEXT]
            piece = doc
    event["piece"] = piece
    return event


class ReviewBody(BaseModel):
    note: str = Field("", max_length=1000)


@ops_router.patch("/events/{event_id}/review")
@limiter.limit("60/minute")
async def review_event(request: Request, event_id: str, body: ReviewBody, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Mark a caught item as looked at, with an optional note."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(event_id)
    except InvalidId:
        raise HTTPException(status_code=404, detail="That safety event was not found.")
    res = await safety_events.update_one(
        {"_id": oid},
        {"$set": {"status": "reviewed", "reviewed_by": str(user.get("id")), "reviewed_at": datetime.now(timezone.utc), "note": body.note.strip()}},
    )
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="That safety event was not found.")
    return {"id": event_id, "status": "reviewed"}


class TryBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT)


@ops_router.post("/try")
@limiter.limit("30/minute")
async def try_text(request: Request, body: TryBody, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """What the free rules say about a text with today's settings. No model call, nothing is saved."""
    settings_doc = await guard_config.ensure_fresh()
    cleaned = clean_text(body.text)
    verdict = rule_screen(cleaned, settings_doc)
    return {
        "cleaned": cleaned,
        "ok": verdict.ok,
        "categories": verdict.categories,
        "category_names": [CATEGORIES.get(c, c) for c in verdict.categories],
        "matches": verdict.matches,
        "ai_phrases": ai_phrases_in(cleaned, settings_doc.get("ai_phrases") or ()),
    }
