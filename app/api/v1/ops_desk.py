"""Ops desk routes: the lookup cards' passive signals, the active brief, and the shared team scratchpad.

Passive: ``GET /signals/...`` is plain code and costs nothing. Active: ``POST /brief`` asks the model, is capped and is kept in the
staff ledger. The scratchpad is one note for the whole Ops team, saved with a version so two people never overwrite each other.
Ticket help and caught-item review use the existing support and safety routes, which already follow the same two-part pattern.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.core.staff import audit, require_action
from app.db.mongo import get_db
from app.ops_desk import brief, signals

router = APIRouter()

SCRATCHPAD_MAX = 4000


def _notes():
    return get_db()["ops_desk_notes"]


@router.get("/signals/workspace/{workspace_id}")
@limiter.limit("120/minute")
async def workspace_card(request: Request, workspace_id: str, staff: dict = Depends(require_action("workspaces", "view"))) -> dict:
    """What is worth knowing about a workspace, by plain rules. No model, no cost."""
    result = await signals.workspace_signals(workspace_id)
    if not result:
        raise HTTPException(status_code=404, detail="That workspace doesn't exist.")
    return result | {"facts": _jsonable(result["facts"])}


@router.get("/signals/person/{user_id}")
@limiter.limit("120/minute")
async def person_card(request: Request, user_id: str, staff: dict = Depends(require_action("workspaces", "view"))) -> dict:
    result = await signals.person_signals(user_id)
    if not result:
        raise HTTPException(status_code=404, detail="That person doesn't exist.")
    return result | {"facts": _jsonable(result["facts"])}


class BriefIn(BaseModel):
    kind: Literal["workspace", "person"]
    id: str = Field(..., min_length=1, max_length=80)


@router.post("/brief")
@limiter.limit("10/minute")
async def brief_me(request: Request, body: BriefIn, staff: dict = Depends(require_action("workspaces", "view"))) -> dict:
    """Active: a few plain sentences about one workspace or person, written when asked. Returns text only and changes nothing."""
    result = await (signals.workspace_signals(body.id) if body.kind == "workspace" else signals.person_signals(body.id))
    if not result:
        raise HTTPException(status_code=404, detail="That one doesn't exist.")
    out = await brief.write_brief(result, staff)
    await audit(staff, "desk.brief", body.id, after={"kind": body.kind, "source": out["source"]})
    return out


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


# ── scratchpad ───────────────────────────────────────────────────────────

async def _scratchpad_out() -> dict:
    doc = await _notes().find_one({"_id": "scratchpad"}) or {}
    updated = doc.get("updated_at")
    return {
        "text": doc.get("text", ""), "version": int(doc.get("version", 0)), "updated_by": doc.get("updated_by_name", ""),
        "updated_at": updated.isoformat() if isinstance(updated, datetime) else None,
    }


@router.get("/scratchpad")
@limiter.limit("120/minute")
async def get_scratchpad(request: Request, staff: dict = Depends(require_platform_staff)) -> dict:
    return await _scratchpad_out()


class ScratchpadIn(BaseModel):
    text: str = Field("", max_length=SCRATCHPAD_MAX)
    # the version this person last saw; a different one means someone saved in between
    version: int = Field(..., ge=0)


@router.put("/scratchpad")
@limiter.limit("60/minute")
async def put_scratchpad(request: Request, body: ScratchpadIn, staff: dict = Depends(require_platform_staff)) -> dict:
    """Save the team's note. If someone else saved since this person loaded it, nothing is overwritten: they get the newer note back."""
    now = datetime.now(timezone.utc)
    update = {"$set": {"text": body.text, "updated_by": staff["id"], "updated_by_name": staff.get("name") or staff.get("email") or "Someone", "updated_at": now},
              "$inc": {"version": 1}}
    if body.version == 0:
        # nothing saved yet: only the first save may create it
        try:
            await _notes().insert_one({"_id": "scratchpad", "text": body.text, "version": 1, "updated_by": staff["id"],
                                       "updated_by_name": staff.get("name") or staff.get("email") or "Someone", "updated_at": now})
            return await _scratchpad_out()
        except Exception:  # noqa: BLE001 - someone saved the first version a moment ago
            pass
    res = await _notes().update_one({"_id": "scratchpad", "version": body.version}, update)
    if not res.matched_count:
        raise HTTPException(status_code=409, detail="Someone else just changed this note. Their version is shown; add your change to it.")
    return await _scratchpad_out()
