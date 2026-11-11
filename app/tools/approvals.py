"""The calls waiting for a person's yes.

A tool that publishes is never run from outside on the caller's word. The call is refused with `approval_required` and an id; at the same
moment it is put here, with the exact thing a person would be saying yes to (the post's real text, where it goes and when), so the person
sees what will happen and not a friendly summary. They answer in Ekavo; a yes makes a one-time token for exactly that call, which the
caller picks up with the id. A request nobody answers is gone after 48 hours."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel

from app.db.mongo import get_db
from app.tools import ledger
from app.tools.contract import ToolSpec
from app.tools.gatekeeper import Credential, fingerprint

PREVIEW_CHARS = 1500


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def preview(workspace_id: str, tool: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """What the person is being asked to approve, read from the real thing the call names."""
    out: dict[str, Any] = {"tool": tool.name, "action": tool.summary, "details": []}
    piece_ids = [arguments["piece_id"]] if isinstance(arguments.get("piece_id"), str) else [str(i) for i in arguments.get("piece_ids") or []]
    if piece_ids:
        rows = await get_db()["content_pieces"].find({"piece_id": {"$in": piece_ids[:10]}, "workspace_id": workspace_id}).to_list(length=10)
        out["posts"] = [{"id": r["piece_id"], "platform": str(r.get("platform") or ""), "text": str(r.get("content") or "")[:PREVIEW_CHARS],
                         "truncated": len(str(r.get("content") or "")) > PREVIEW_CHARS} for r in rows]
        out["more_posts"] = max(0, len(piece_ids) - len(rows))
    for key in ("scheduled_at", "destination", "language", "title"):
        if arguments.get(key):
            out["details"].append({"label": key.replace("_", " ").capitalize(), "value": str(arguments[key])})
    return out


async def open_request(credential: Credential, tool: ToolSpec, arguments: BaseModel) -> Optional[str]:
    """Record the call for a person to answer; the same call asked again finds the same open request."""
    data = arguments.model_dump(mode="json", exclude={"confirm"})
    fp = fingerprint(tool.name, data)
    found = await ledger.approval_requests().find_one(
        {"workspace_id": credential.workspace_id, "credential_id": credential.id, "fingerprint": fp, "status": "pending"}, {"id": 1})
    if found:
        return found["id"]
    row = {"id": uuid4().hex, "workspace_id": credential.workspace_id, "credential_id": credential.id, "surface": credential.surface, "tool": tool.name,
           "arguments": data, "fingerprint": fp, "status": "pending", "created_at": _now(), "preview": await preview(credential.workspace_id, tool, data)}
    await ledger.approval_requests().insert_one(dict(row))
    return row["id"]


def view(row: dict[str, Any], *, with_token: bool = False) -> dict[str, Any]:
    out = {"id": row["id"], "tool": row["tool"], "status": row["status"], "surface": row.get("surface"), "credential_id": row.get("credential_id"),
           "created_at": row["created_at"].isoformat().replace("+00:00", "Z"), "preview": row.get("preview") or {}, "decided_by": row.get("decided_by")}
    if with_token and row["status"] == "approved":
        out["approval_token"] = row.get("token")
    return out


async def pending(workspace_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = await ledger.approval_requests().find({"workspace_id": workspace_id, "status": "pending"}, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(length=limit)
    return [view(r) for r in rows]


async def decide(workspace_id: str, request_id: str, user_id: str, approve: bool) -> Optional[dict[str, Any]]:
    """A person's answer. A yes makes the one-time token for exactly this call; a no ends the request. Answered once."""
    row = await ledger.approval_requests().find_one({"id": request_id, "workspace_id": workspace_id}, {"_id": 0})
    if row is None:
        return None
    if row["status"] != "pending":
        return view(row)
    if approve:
        token = await ledger.issue_approval(workspace_id, user_id, row["tool"], row["fingerprint"])
        update = {"status": "approved", "token": token, "decided_by": user_id, "decided_at": _now()}
    else:
        update = {"status": "declined", "decided_by": user_id, "decided_at": _now()}
    taken = await ledger.approval_requests().find_one_and_update(
        {"id": request_id, "workspace_id": workspace_id, "status": "pending"}, {"$set": update}, return_document=True, projection={"_id": 0})
    return view(taken or row)


async def poll(credential: Credential, request_id: str) -> Optional[dict[str, Any]]:
    """What the caller of the call is told: still waiting, declined, or approved with the token to repeat the call with."""
    row = await ledger.approval_requests().find_one({"id": request_id, "workspace_id": credential.workspace_id, "credential_id": credential.id}, {"_id": 0})
    return view(row, with_token=True) if row else None
