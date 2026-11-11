"""What connected apps, keys and assistants changed in this workspace, and a way for a person to take a change back from the app.

This is the person's side of `changes.list` and `changes.undo`: the same records and the same undo, done as the signed-in member with their
own role. Taking a change back goes through the gatekeeper like any other call, so it is checked, recorded, and can itself be undone."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.tools import changes as changes_tools
from app.tools import executor, undo
from app.tools.gatekeeper import Call, Gatekeeper, Refusal
from app.tools.registry import get_registry
from app.tools.services import LiveServices, member_credential_id

router = APIRouter()


@router.get("")
@limiter.limit("120/minute")
async def list_changes(
    request: Request, limit: int = Query(20, ge=1, le=50), cursor: Optional[str] = Query(None, max_length=200),
    target_id: Optional[str] = Query(None, max_length=80), ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    q: dict[str, Any] = {"workspace_id": ctx.workspace_id}
    if target_id:
        q["target.id"] = target_id
    if cursor:
        try:
            when, last = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="That cursor is not valid. Start again without one.")
        q["$or"] = [{"at": {"$lt": moment}}, {"at": moment, "id": {"$lt": last}}]
    rows = await undo.changes().find(q, {"_id": 0, "before": 0}).sort([("at", -1), ("id", -1)]).limit(limit + 1).to_list(length=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = base64.urlsafe_b64encode(json.dumps([rows[-1]["at"].isoformat(), rows[-1]["id"]]).encode("utf-8")).decode("ascii") if more and rows else None
    registry = get_registry()
    items = []
    for r in rows:
        tool = registry.get(r["tool"] if r["tool"] != "changes.undo" else r.get("undo_rule_tool", ""))
        items.append({**changes_tools._view(r), "summary": tool.summary if tool else r["tool"], "by_key": r.get("surface") == "api"})
    return {"items": items, "next_cursor": nxt}


@router.post("/{change_id}/undo")
@limiter.limit("30/minute")
async def undo_change(request: Request, change_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    gate = Gatekeeper(get_registry(), LiveServices())
    call = Call(credential_id=member_credential_id(ctx.workspace_id, ctx.user_id), tool="changes.undo", arguments={"change_id": change_id})
    try:
        _, outcome = await executor.run_call(gate, call)
    except Refusal as refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.message)
    except executor.ToolFailure as failure:
        raise HTTPException(status_code=failure.status, detail=failure.message)
    return {"result": outcome.data if outcome else {}}
