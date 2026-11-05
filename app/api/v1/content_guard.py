"""Content Guard routes.

Members: check a piece of text and get back the cleaned or rewritten version.
Ops (`/api/v1/ops/content-safety`): read and change the settings, see what was blocked or rewritten, and try a text against
the rules. Settings apply to every workspace, so changing them is for the Ops owner and reading them is for platform staff.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.agents.content_guard import config as guard_config
from app.agents.content_guard import appeals
from app.agents.content_guard import input_guard
from app.agents.content_guard import judges
from app.agents.content_guard import ladder
from app.agents.content_guard import profiles
from app.agents.content_guard import term_lists
from app.agents.content_guard.agent import review_text, rule_screen
from app.agents.content_guard.rules import CATEGORIES, ai_phrases_in, clean_text
from app.core.auth import require_platform_staff
from app.core.staff import require_action
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, require
from app.db.mongo import safety_events
from app.shared.activity import record_system

logger = logging.getLogger(__name__)

router = APIRouter()
ops_router = APIRouter()

MAX_TEXT = 8000


class CheckBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT)
    piece_id: Optional[str] = None
    platform: Optional[str] = None
    rewrite: bool = True
    language: Optional[str] = Field(None, max_length=20)


@router.post("/check")
@limiter.limit("20/minute")
async def check_text(request: Request, body: CheckBody, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    """The text with dashes and filler removed, rewritten when it was flagged and could be made safe, or marked blocked."""
    result = await review_text(
        body.text, where="check", workspace_id=ctx.workspace_id, piece_id=body.piece_id, platform=body.platform,
        allow_rewrite=body.rewrite, language=body.language,
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
        "judges": [{"key": k, "label": judges.JUDGE_LABELS[k]} for k in judges.JUDGES],
        "profile_defs": profiles.catalogue(settings_doc),
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
    judge_models: Optional[list[str]] = None
    shadow_model: Optional[str] = None
    shadow_rate: Optional[float] = Field(None, ge=0.0, le=1.0)
    profiles: Optional[dict[str, dict[str, Any]]] = None
    terms_by_language: Optional[dict[str, dict[str, list[str]]]] = None
    reason: str = Field("", max_length=400)


class AskReviewBody(BaseModel):
    piece_id: str = Field(..., max_length=80)
    note: str = Field("", max_length=400)


@router.post("/review-requests")
@limiter.limit("20/minute")
async def ask_for_review(request: Request, body: AskReviewBody, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    """A member asks for a person to look at a post the safety check held back."""
    from app.db.mongo import content_pieces

    piece = await content_pieces.find_one({"piece_id": body.piece_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}})
    if not piece:
        raise HTTPException(status_code=404, detail="That post was not found.")
    try:
        return await appeals.request_review(piece, user_id=ctx.user_id, note=body.note)
    except appeals.AppealError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/review-requests/{piece_id}")
@limiter.limit("60/minute")
async def my_review_request(request: Request, piece_id: str, ctx: WorkspaceContext = Depends(require("edit_content"))) -> dict[str, Any]:
    """The latest review request for a post and its answer, or none."""
    return {"request": await appeals.mine(piece_id, ctx.workspace_id)}


class DecideBody(BaseModel):
    decision: Literal["release", "uphold"]
    note: str = Field("", max_length=400)
    suggest_word: str = Field("", max_length=80)


@ops_router.get("/appeals")
@limiter.limit("60/minute")
async def list_appeals(request: Request, status: Optional[str] = Query(None, max_length=10), user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return await appeals.listing(status)


@ops_router.post("/appeals/{appeal_id}/decide")
@limiter.limit("60/minute")
async def decide_appeal(request: Request, appeal_id: str, body: DecideBody, user: dict = Depends(require_action("safety", "approve"))) -> dict[str, Any]:
    """Release a held post for sending exactly as written, or uphold the hold. Needs approval rights on Safety."""
    try:
        answered = await appeals.decide(appeal_id, decision=body.decision, note=body.note, staff=user)
        if body.decision == "uphold" and body.suggest_word.strip():
            await term_lists.suggest(body.suggest_word, answered.get("language"), source="review", by=str(user.get("id")), note=body.note, example=answered.get("excerpt") or "")
        return answered
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except appeals.AppealError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


class SuggestBody(BaseModel):
    term: str = Field(..., max_length=80)
    language: str = Field("all", max_length=12)
    note: str = Field("", max_length=200)


class SeedBody(BaseModel):
    words: list[str] = Field(..., max_length=200)
    language: str = Field("all", max_length=12)


class SuggestionDecision(BaseModel):
    decision: Literal["accept", "reject"]


@ops_router.get("/suggestions")
@limiter.limit("60/minute")
async def list_suggestions(request: Request, status: str = Query("pending", max_length=10), user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return await term_lists.listing(status)


@ops_router.post("/suggestions")
@limiter.limit("60/minute")
async def add_suggestion(request: Request, body: SuggestBody, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Suggest a word for the rules. It waits in the queue until someone with approval rights accepts it."""
    try:
        return await term_lists.suggest(body.term, body.language, source="staff", by=str(user.get("id")), note=body.note)
    except term_lists.SuggestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@ops_router.post("/suggestions/import")
@limiter.limit("10/minute")
async def import_suggestions(request: Request, body: SeedBody, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """A pasted word list goes into the queue as suggestions. Nothing reaches the rules until it is accepted."""
    return await term_lists.import_seeds(body.words, body.language, by=str(user.get("id")))


@ops_router.post("/suggestions/{suggestion_id}/decide")
@limiter.limit("60/minute")
async def decide_suggestion(request: Request, suggestion_id: str, body: SuggestionDecision, user: dict = Depends(require_action("safety", "approve"))) -> dict[str, Any]:
    """Accept a suggested word, which adds it to the rules and is kept in the history, or reject it so it is not suggested again."""
    try:
        return await term_lists.decide(suggestion_id, decision=body.decision, staff_id=str(user["id"]), staff_name=user.get("name") or user.get("email"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except term_lists.SuggestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except guard_config.VersionConflict:
        raise HTTPException(status_code=409, detail="The rules changed while you were answering. Try again.")


class LadderBody(BaseModel):
    levels: dict[str, int] = Field(default_factory=dict)
    enabled: Optional[bool] = None
    reason: str = Field("", max_length=400)
    version: int


@ops_router.get("/ladder")
@limiter.limit("60/minute")
async def get_ladder(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """How much authority the model's reading has for each kind of problem, whether it has earned a step up, and the switch."""
    return await ladder.snapshot()


@ops_router.put("/ladder")
@limiter.limit("20/minute")
async def save_ladder(request: Request, body: LadderBody, user: dict = Depends(require_action("safety", "approve"))) -> dict[str, Any]:
    try:
        return await ladder.save(levels=body.levels, enabled=body.enabled, reason=body.reason, expected_version=body.version, user_id=str(user["id"]), user_name=user.get("name") or user.get("email"))
    except ladder.LadderError as exc:
        raise HTTPException(status_code=409 if "changed while" in str(exc) else 422, detail=str(exc))


@ops_router.get("/ladder/history")
@limiter.limit("60/minute")
async def ladder_history(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return {"items": await ladder.history()}


@ops_router.get("/overrides")
@limiter.limit("60/minute")
async def list_overrides(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return {"items": await appeals.overrides()}


@ops_router.get("/rule-results")
@limiter.limit("60/minute")
async def rule_results(request: Request, days: int = Query(30), user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return await appeals.rule_results(days)


@ops_router.get("/history")
@limiter.limit("60/minute")
async def settings_history(request: Request, limit: int = Query(50, ge=1, le=200), user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return {"items": await guard_config.history(limit)}


class RestoreBody(BaseModel):
    version: int


@ops_router.post("/history/{history_id}/restore")
@limiter.limit("20/minute")
async def restore_settings(request: Request, history_id: str, body: RestoreBody, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    try:
        saved = await guard_config.restore(history_id, expected_version=body.version, user_id=str(user["id"]), user_name=user.get("name") or user.get("email"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except guard_config.VersionConflict:
        raise HTTPException(status_code=409, detail="These settings changed since you opened them. Reload and try again.")
    return _public(saved)


@ops_router.get("/input-flags")
@limiter.limit("60/minute")
async def input_flags(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Sources that contained orders aimed at the writing assistant, newest first. They were used as material only."""
    return {"items": await input_guard.recent()}


@ops_router.get("/judges")
@limiter.limit("30/minute")
async def judge_comparison(request: Request, days: int = Query(30, ge=1, le=90), user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """How often the silent second model agrees with the one that answers, by language, and the latest disagreements."""
    return await judges.comparison_summary(days)


@ops_router.get("")
@limiter.limit("30/minute")
async def get_settings(request: Request, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    return _public(await guard_config.load())


@ops_router.put("")
@limiter.limit("20/minute")
async def save_settings(request: Request, body: SettingsBody, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True, exclude={"version", "reason"})
    if len(" ".join(body.reason.split())) < 4:
        raise HTTPException(status_code=422, detail="Say in a few words why you are changing this.")
    try:
        saved = await guard_config.save(changes, expected_version=body.version, user_id=str(user["id"]), reason=body.reason, user_name=user.get("name") or user.get("email"))
    except guard_config.VersionConflict:
        raise HTTPException(status_code=409, detail="These settings changed since you opened them. Reload and try again.")
    workspace_id = user.get("default_workspace_id")
    if workspace_id:
        try:
            await record_system(
                workspace_id=workspace_id,
                key=f"contentsafety:{uuid4()}",
                actor_name="Ekavo staff",
                actor_user_id=str(user["id"]),
                category="platform_ops",
                title="Content safety settings saved",
                description="Changed: " + ", ".join(sorted(changes)),
                visibility="admins",
                metadata={"event": "content_safety.saved", "changed": sorted(changes), "version": saved.get("version")},
            )
        except Exception as exc:  # noqa: BLE001 - the settings are saved; only the Activity Log entry is missing
            logger.warning("Could not write the Activity Log entry for the Content safety settings change: %s", exc)
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
        async for row in safety_events.find(flt, {"fingerprint": 0}).sort([("created_at", -1), ("_id", -1)]).skip(offset).limit(limit)
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
async def review_event(request: Request, event_id: str, body: ReviewBody, user: dict = Depends(require_action("safety", "approve"))) -> dict[str, Any]:
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
