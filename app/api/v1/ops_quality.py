"""Ops, Quality (`/api/v1/ops/quality`): the writing checks, auto-publish trust and pattern numbers, what the checks held back, a way
to try a text, and the history of changes. Mounted behind the Quality area: reading needs view, any change needs edit. The trust
numbers decide what may publish without a person, so only a Master Admin changes them.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.models.text import Platform
from app.pipelines.text.quality import run_quality_gate
from app.quality import defaults as D
from app.quality import held
from app.quality import reference
from app.quality import signals
from app.quality import settings as QS
from app.shared.activity import record_system

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_TEXT = 12000


class SaveBody(BaseModel):
    changes: dict[str, Any]
    reason: str = Field("", max_length=400)
    version: int = Field(..., ge=0)


class ResetBody(BaseModel):
    reason: str = Field("", max_length=400)
    version: int = Field(..., ge=0)


class RestoreBody(BaseModel):
    version: int = Field(..., ge=0)


class TryBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT)
    platform: str = Field(..., max_length=40)
    language: str = Field("en", max_length=20)
    changes: Optional[dict[str, Any]] = None


def _name(user: dict) -> str:
    return str(user.get("name") or user.get("email") or "Staff")[:80]


def _is_master(user: dict) -> bool:
    return bool(user.get("is_master_admin"))


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, QS.VersionConflict):
        return HTTPException(status_code=409, detail="These settings changed since you opened them. Reload and try again.")
    return HTTPException(status_code=422, detail=str(exc))


async def _note(user: dict, title: str, description: str, event: str, diffs: list[dict]) -> None:
    workspace_id = user.get("default_workspace_id")
    if not workspace_id:
        return
    try:
        await record_system(
            workspace_id=workspace_id, key=f"quality:{uuid4()}", actor_name="Ekavo staff", actor_user_id=str(user["id"]),
            category="platform_ops", title=title, description=description, visibility="admins",
            metadata={"event": event, "changed": [d["path"] for d in diffs]},
        )
    except Exception as exc:  # noqa: BLE001 - the change is saved; only the Activity Log entry is missing
        logger.warning("Could not write the Activity Log entry for a Quality change: %s", exc)


@router.get("")
async def get_settings(request: Request, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    await QS.load()
    return {**QS.public(), "can_edit_trust": _is_master(user)}


@router.put("")
@limiter.limit("30/minute")
async def save_settings(request: Request, body: SaveBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    if QS.touches_trust(body.changes) and not _is_master(user):
        raise HTTPException(status_code=403, detail="Only a Master Admin changes the auto-publish trust numbers.")
    try:
        await QS.save(body.changes, reason=body.reason, expected_version=body.version, user_id=str(user["id"]), user_name=_name(user))
    except (QS.SettingsError, QS.VersionConflict) as exc:
        raise _fail(exc)
    last = (await QS.history(1))[0]
    await _note(user, "Quality settings saved", f"{last['reason']} ({len(last['changes'])} changed)", "quality.saved", last["changes"])
    return {**QS.public(), "can_edit_trust": _is_master(user)}


@router.post("/reset")
@limiter.limit("10/minute")
async def reset(request: Request, body: ResetBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    if not _is_master(user):
        raise HTTPException(status_code=403, detail="Only a Master Admin puts every quality setting back to its starting value.")
    try:
        await QS.reset_all(reason=body.reason, expected_version=body.version, user_id=str(user["id"]), user_name=_name(user))
    except (QS.SettingsError, QS.VersionConflict) as exc:
        raise _fail(exc)
    last = (await QS.history(1))[0]
    await _note(user, "Quality settings reset", last["reason"], "quality.reset", last["changes"])
    return {**QS.public(), "can_edit_trust": True}


@router.get("/history")
async def history(request: Request, limit: int = Query(50, ge=1, le=200), before: Optional[datetime] = None, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    rows = await QS.history(limit, before)
    return {"items": rows, "more": len(rows) >= limit}


@router.post("/history/{history_id}/restore")
@limiter.limit("20/minute")
async def restore(request: Request, history_id: str, body: RestoreBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    rows = [r for r in await QS.history(QS.HISTORY_LIMIT) if r["id"] == history_id]
    if rows and any(c["path"].startswith("trust.") for c in rows[0]["changes"]) and not _is_master(user):
        raise HTTPException(status_code=403, detail="Only a Master Admin undoes a change to the auto-publish trust numbers.")
    try:
        await QS.restore(history_id, expected_version=body.version, user_id=str(user["id"]), user_name=_name(user))
    except (QS.SettingsError, QS.VersionConflict) as exc:
        raise _fail(exc)
    last = (await QS.history(1))[0]
    await _note(user, "Quality change undone", last["reason"], "quality.restored", last["changes"])
    return {**QS.public(), "can_edit_trust": _is_master(user)}


@router.get("/summary")
async def summary(request: Request, days: int = Query(30), user: dict = Depends(get_current_user)) -> dict[str, Any]:
    await QS.load()
    data = await held.summary(days)
    data["edited"] = len(QS.edited_paths())
    data["fields"] = len(QS._flatten(QS.defaults()))  # noqa: SLF001 - how many fields there are in all
    return data


@router.get("/held")
async def held_posts(
    request: Request, days: int = Query(30), platform: Optional[str] = Query(None, max_length=40), check: Optional[str] = Query(None, max_length=30),
    page: int = Query(0, ge=0, le=200), user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    if check and check not in held.CHECK_LABEL:
        raise HTTPException(status_code=422, detail="That check does not exist.")
    return await held.held_list(days=days if days in held.DAY_CHOICES else 30, platform=platform, check=check, page=page)


@router.get("/held/{piece_id}")
async def held_post(request: Request, piece_id: str, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    row = await held.held_one(piece_id)
    if not row:
        raise HTTPException(status_code=404, detail="That post is not held, or no longer exists.")
    return row


@router.get("/signals")
async def quality_signals(request: Request, days: int = Query(30), user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """How members treat what Ekavo writes: what they keep as it was, edit lightly or heavily, and why they send posts back."""
    return await signals.summary(days)


class ExampleBody(BaseModel):
    platform: str = Field(..., max_length=40)
    language: str = Field("en", max_length=20)
    text: str = Field(..., max_length=reference.TEXT_MAX + 1000)
    expect: str = Field(..., max_length=10)
    note: str = Field("", max_length=300)


class RunBody(BaseModel):
    changes: Optional[dict[str, Any]] = None


@router.get("/reference")
async def reference_list(request: Request, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"items": await reference.listing(), "limits": {"total": reference.TOTAL_MAX, "per_platform": reference.PER_PLATFORM_MAX}}


@router.post("/reference")
@limiter.limit("60/minute")
async def reference_add(request: Request, body: ExampleBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return await reference.add(platform=body.platform, language=body.language, text=body.text, expect=body.expect, note=body.note, user_id=str(user["id"]))
    except reference.ReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/reference/{example_id}")
@limiter.limit("60/minute")
async def reference_remove(request: Request, example_id: str, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    if not await reference.remove(example_id):
        raise HTTPException(status_code=404, detail="That draft is not in the set.")
    return {"removed": example_id}


@router.post("/reference/run")
@limiter.limit("20/minute")
async def reference_run(request: Request, body: RunBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Run every saved draft through the free checks with today's values, or with changes not saved yet. No model is called."""
    try:
        return await reference.run(body.changes)
    except QS.SettingsError as exc:
        raise _fail(exc)


@router.get("/distribution")
async def distribution(request: Request, platform: str = Query(..., max_length=40), days: int = Query(30), user: dict = Depends(get_current_user)) -> dict[str, Any]:
    if platform not in D.PLATFORMS:
        raise HTTPException(status_code=422, detail="That is not a platform.")
    return await held.distribution(platform, days if days in held.DAY_CHOICES else 30)


@router.post("/try")
@limiter.limit("30/minute")
async def try_text(request: Request, body: TryBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Run the writing checks over a text with today's values, or with unsaved ones. Nothing is saved and no model is called."""
    if body.platform not in D.PLATFORMS:
        raise HTTPException(status_code=422, detail="That is not a platform.")
    live = await QS.load()
    values = live
    if body.changes:
        try:
            tidy = QS.clean_changes(body.changes, live)
        except QS.SettingsError as exc:
            raise _fail(exc)
        values = deepcopy(live)
        for group in ("min_words", "readability", "trust", "patterns"):
            values[group].update(tidy.get(group) or {})
        for name in QS.LISTS:
            if name in tidy:
                values[name] = tidy[name]
    platform = Platform(body.platform)
    with QS.trying(values):
        result = await run_quality_gate(
            body.text, platform, brand_context="", banned_words=[], avoid_blacklist=False, grammar_check=True, language=body.language or "en",
        )
        bar, minimum = QS.readability_bar(body.platform), QS.min_words(body.platform)
    words = len(body.text.split())
    checks = []
    for issue in result.issues:
        advice = issue.lower().startswith("advisory:")
        checks.append({"state": "advice" if advice else "held", "check": held.classify(issue), "text": issue.removeprefix("Advisory: ").removeprefix("advisory: ")})
    return {
        "passed": result.passed, "using": "your unsaved values" if body.changes else "the saved values",
        "words": words, "minimum_words": minimum, "readability": result.readability_score, "readability_bar": bar,
        "checks": checks,
    }
