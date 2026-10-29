"""Typing suggestions for text boxes. See app/pipelines/suggest.py for how they are made and why they cost almost nothing.

Both routes only ever read the caller's own earlier work and their workspace's brand, and both answer "no suggestion" instead of an
error when something is off, so a text box never shows a failure for a suggestion that could not be made."""
import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import audio_assets, brand_profiles, content_sessions, get_db, image_assets
from app.pipelines import suggest as engine
from app.shared.llm import GroqModel, call_llm, set_usage_workspace

logger = logging.getLogger(__name__)
router = APIRouter()

Kind = Literal["post_idea", "image_prompt", "image_text", "description", "script", "post_edit", "instruction", "brand_text"]

_MAX_ITEMS = 120
_recent_calls: deque = deque()  # when each AI continuation was asked for, to keep the whole app inside its free allowance


def _one_line(text: object, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def _brand_terms(brand: dict) -> list[str]:
    """The short phrases a brand is described with: its niche, tagline, pillars, features and audience words."""
    found: list[str] = []

    def take(value: object) -> None:
        if isinstance(value, str):
            text = _one_line(value, 120)
            if 3 <= len(text) <= 120:
                found.append(text)
        elif isinstance(value, list):
            for item in value:
                take(item)

    for key in ("niche", "tagline", "core_message", "content_pillars", "industry", "mission", "target_market", "category", "one_liner", "problem_solved", "key_features"):
        take((brand.get("identity") or {}).get(key))
    audience = brand.get("audience") or {}
    take(audience.get("primary_pain_point"))
    for value in (audience.get("extra") or {}).values():
        take(value)
    return found


async def _enabled(ctx: WorkspaceContext) -> bool:
    return ctx.user.get("prompt_suggestions", True) is not False


@router.get("/corpus")
@limiter.limit("60/minute")
async def corpus(
    request: Request,
    kind: Kind = Query(...),
    brand_id: Optional[str] = Query(default=None, max_length=64),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The words a member's own earlier work and their brand offer for this kind of text box, to be matched in the browser while
    typing. Only the caller's own work is read, never a teammate's."""
    if not await _enabled(ctx):
        return {"enabled": False, "items": []}

    items: list[dict] = []
    seen: set[str] = set()

    def add(text: object, source: str) -> None:
        line = _one_line(text)
        key = line.lower()
        if len(line) >= 4 and key not in seen and len(items) < _MAX_ITEMS:
            seen.add(key)
            items.append({"text": line, "source": source})

    try:
        scope = {"workspace_id": ctx.workspace_id}
        if kind in ("post_idea", "post_edit", "instruction", "description"):
            rows = await content_sessions.find(
                {**scope, "user_id": ctx.user_id, "source_type": "topic"}, {"input_text": 1},
            ).sort("created_at", -1).limit(40).to_list(length=40)
            for row in rows:
                add(row.get("input_text"), "history")
        elif kind in ("image_prompt", "image_text"):
            rows = await image_assets.find(
                {**scope, "created_by": ctx.user_id, "deleted": {"$ne": True}, "source_type": "ai_generated"}, {"prompt": 1, "slides.text_content": 1},
            ).sort("created_at", -1).limit(40).to_list(length=40)
            for row in rows:
                if kind == "image_prompt":
                    add(row.get("prompt"), "history")
                else:
                    for slide in (row.get("slides") or [])[:1]:
                        add((slide.get("text_content") or {}).get("headline"), "history")
        elif kind == "script":
            rows = await audio_assets.find(
                {**scope, "created_by": ctx.user_id, "deleted": {"$ne": True}, "script": {"$nin": [None, ""]}}, {"script": 1},
            ).sort("created_at", -1).limit(20).to_list(length=20)
            for row in rows:
                add(row.get("script"), "history")

        if brand_id:
            brand = await brand_profiles.find_one({"id": brand_id, "workspace_id": ctx.workspace_id})
        else:
            brand = await brand_profiles.find_one({"workspace_id": ctx.workspace_id, "is_default": True}) or await brand_profiles.find_one({"workspace_id": ctx.workspace_id})
        if brand:
            for term in _brand_terms(brand):
                add(term, "brand")
    except Exception as exc:  # noqa: BLE001 - a suggestion that cannot be made is simply not shown
        logger.info("Suggestion corpus could not be read: %s", exc)
    return {"enabled": True, "items": items}


class CompleteRequest(BaseModel):
    kind: Kind
    # The text before the cursor. Only the end of it is used.
    text: str = Field(min_length=1, max_length=4000)
    brand_id: Optional[str] = Field(default=None, max_length=64)


async def _allowed_today(user_id: str) -> bool:
    """Counts one continuation against the member's daily allowance; False once it is used up."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = await get_db()["suggest_usage"].find_one_and_update(
        {"user_id": user_id, "date": today}, {"$inc": {"count": 1}, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True, return_document=True,
    )
    return int((row or {}).get("count", 1)) <= settings.SUGGEST_DAILY_CAP


def _app_has_room() -> bool:
    now = time.monotonic()
    while _recent_calls and now - _recent_calls[0] > 60:
        _recent_calls.popleft()
    if len(_recent_calls) >= settings.SUGGEST_APP_PER_MINUTE:
        return False
    _recent_calls.append(now)
    return True


@router.post("/complete")
@limiter.limit("40/minute")
async def complete(request: Request, body: CompleteRequest, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict:
    """One short continuation of what is typed, or `{"completion": null}`. Never an error."""
    nothing = {"completion": None, "source": None}
    typed = body.text[-600:]
    if not await _enabled(ctx) or len(typed.strip()) < engine.MIN_CHARS_FOR_AI:
        return nothing

    key = engine.cache_key(ctx.user_id, body.kind, typed)
    found, value = engine.cached(key)
    if found:
        return {"completion": value, "source": "ai" if value else None}

    try:
        from app.shared.llm import set_usage_workspace

        # typing suggestions are always free and never switch off with the credits; they keep their own daily limit
        set_usage_workspace(ctx.workspace_id)
        if not await _allowed_today(ctx.user_id) or not _app_has_room():
            return nothing
        brand_hint = ""
        if body.brand_id:
            brand = await brand_profiles.find_one({"id": body.brand_id, "workspace_id": ctx.workspace_id})
            if brand:
                brand_hint = "; ".join(_brand_terms(brand)[:3])[:200]
        system, user = engine.prompt_for(body.kind, typed, brand_hint)
        set_usage_workspace(ctx.workspace_id)
        raw = await asyncio.wait_for(
            call_llm(user, model=GroqModel.FAST, system=system, temperature=0.4, max_tokens=160, reasoning_effort="low"), timeout=6.0,
        )
        completion = engine.clean_completion(raw, typed)
    except Exception as exc:  # noqa: BLE001 - includes a spent allowance, a busy model and a timeout
        logger.info("Suggestion not made: %s", exc)
        return nothing
    engine.remember(key, completion)
    return {"completion": completion, "source": "ai" if completion else None}
