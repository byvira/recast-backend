"""One-tap links: a decision that comes to the person instead of the person going to a page.

A link is made for one person, one action and one thing (approve this post, send it back, say yes or no to a call waiting for a yes). It
works once and for 48 hours. The token is random and only its hash is kept, so a leaked database cannot be used to act.

Opening a link only SHOWS what it would do: mail scanners and chat apps open every link in a message, so opening can never act. The action
happens only when the person presses the button on the page, which sends a second request. The action then runs through the same gatekeeper
as any other call, as that person with their current role, so a person who was demoted or removed since the link was made cannot use it."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.core.config import settings
from app.db.mongo import get_db
from app.tools import approvals, executor, ledger
from app.tools.gatekeeper import Call, Gatekeeper, Refusal
from app.tools.registry import get_registry
from app.tools.services import LiveServices

LINK_HOURS = 48

#: link action -> (what it says on the page, the button, the tool it runs or "decide")
ACTIONS: dict[str, dict[str, str]] = {
    "approve_post": {"title": "Approve this post", "button": "Yes, approve it", "tool": "library.approve_post", "done": "Approved. It can now be scheduled or published."},
    "send_back": {"title": "Send this post back", "button": "Send it back", "tool": "library.reject_post", "done": "Sent back. The writer will see it."},
    "yes_send": {"title": "Let this go out", "button": "Yes, send it", "tool": "decide", "done": "Approved. It goes out now."},
    "no_send": {"title": "Do not send this", "button": "No, do not send", "tool": "decide", "done": "Declined. Nothing was sent."},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def links():
    return get_db()["tool_links"]


def link_credential_id(workspace_id: str, user_id: str) -> str:
    return f"link:{workspace_id}:{user_id}"


async def create_indexes() -> None:
    await links().create_index("hash", unique=True)
    await links().create_index("expires_at", expireAfterSeconds=0)


def url_for(token: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/l/{token}"


async def make(workspace_id: str, user_id: str, action: str, target_id: str) -> str:
    """A link for this person and this one thing. Returns the address to put in the message."""
    if action not in ACTIONS:
        raise ValueError(f"There is no link action {action!r}.")
    token = secrets.token_urlsafe(32)
    await links().insert_one({
        "id": uuid4().hex, "hash": _hash(token), "workspace_id": workspace_id, "user_id": user_id, "action": action, "target_id": target_id,
        "used": False, "created_at": _now(), "expires_at": _now() + timedelta(hours=LINK_HOURS),
    })
    return url_for(token)


async def _live(token: str) -> Optional[dict[str, Any]]:
    if not token or len(token) > 100:
        return None
    return await links().find_one({"hash": _hash(token), "used": False, "expires_at": {"$gt": _now()}}, {"_id": 0})


async def show(token: str) -> Optional[dict[str, Any]]:
    """What the link would do, for the page to show. Changes nothing, so scanners that open it do no harm. The same words for a link that
    never existed, ended or was used, so nothing is learned from guessing."""
    row = await _live(token)
    if row is None:
        return None
    info = ACTIONS[row["action"]]
    out: dict[str, Any] = {"title": info["title"], "button": info["button"], "expires_at": row["expires_at"].isoformat().replace("+00:00", "Z")}
    if row["action"] in ("approve_post", "send_back"):
        piece = await get_db()["content_pieces"].find_one({"piece_id": row["target_id"], "workspace_id": row["workspace_id"]})
        out["post"] = {"platform": str((piece or {}).get("platform") or ""), "text": str((piece or {}).get("content") or "")[:1500]} if piece else None
    else:
        request = await ledger.approval_requests().find_one({"id": row["target_id"], "workspace_id": row["workspace_id"]}, {"_id": 0})
        out["post"] = None
        out["preview"] = (request or {}).get("preview")
        out["waiting"] = bool(request and request.get("status") == "pending")
    return out


class LinkFailed(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


async def do(token: str) -> dict[str, Any]:
    """The button was pressed: use the link up, once, and do what it says as that person."""
    row = await links().find_one_and_update(
        {"hash": _hash(token), "used": False, "expires_at": {"$gt": _now()}}, {"$set": {"used": True, "used_at": _now()}}, projection={"_id": 0})
    if row is None:
        raise LinkFailed("link_invalid", "This link has already been used, or it has ended.", 410)
    info = ACTIONS[row["action"]]
    try:
        if info["tool"] == "decide":
            done = await approvals.decide(row["workspace_id"], row["target_id"], row["user_id"], row["action"] == "yes_send")
            if done is None:
                raise LinkFailed("not_found", "That request is no longer there.", 404)
            return {"message": info["done"] if done["status"] != "pending" else "Nothing changed."}
        gate = Gatekeeper(get_registry(), LiveServices())
        call = Call(credential_id=link_credential_id(row["workspace_id"], row["user_id"]), tool=info["tool"], arguments={"piece_id": row["target_id"]})
        await executor.run_call(gate, call)
        return {"message": info["done"]}
    except Refusal as refusal:
        await _release(row)
        raise LinkFailed(refusal.code, refusal.message, refusal.status)
    except executor.ToolFailure as failure:
        await _release(row)
        raise LinkFailed(failure.code, failure.message, failure.status)


async def _release(row: dict[str, Any]) -> None:
    """The action was refused (the person's role changed, the post is gone), so the link is not used up for nothing: it can be pressed again
    until it ends."""
    await links().update_one({"id": row["id"]}, {"$set": {"used": False}, "$unset": {"used_at": ""}})
