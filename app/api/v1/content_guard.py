"""Content Guard routes.

Members: check a piece of text and get back the cleaned or rewritten version.
Ops (`/api/v1/ops/content-safety`): read and change the settings, see what was blocked or rewritten, and try a text against
the rules. Settings apply to every workspace, so changing them is for the Ops owner and reading them is for platform staff.
"""

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


@ops_router.get("/events")
@limiter.limit("30/minute")
async def list_events(
    request: Request,
    outcome: Optional[Literal["blocked", "rewritten"]] = None,
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    flt: dict[str, Any] = {"outcome": outcome} if outcome else {}
    rows = []
    async for row in safety_events.find(flt, {"_id": 0, "fingerprint": 0}).sort("created_at", -1).limit(limit):
        row["category_names"] = [CATEGORIES.get(c, c) for c in row.get("categories", [])]
        rows.append(row)
    return {"events": rows, "total": await safety_events.count_documents(flt)}


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
