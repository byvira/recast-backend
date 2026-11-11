"""The digest: one email that brings what needs a person to the person, with a button for each decision.

For each member who switched it on (and may approve), it lists the drafts waiting for them and the calls from keys or assistants waiting for
a yes, each with a one-tap link (see links.py). A morning with nothing waiting sends nothing. It is off until the person turns it on, and
one email goes out a day at most. It counts and links only; no model is called."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.notifications import send_templated_email
from app.core.rbac import ROLE_PERMISSIONS
from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_db
from app.tools import ledger, links

logger = logging.getLogger(__name__)

MAX_ITEMS = 5
MAX_PEOPLE = 3000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def prefs():
    return get_db()["tool_digest_prefs"]


async def set_enabled(workspace_id: str, user_id: str, enabled: bool) -> None:
    await prefs().update_one({"workspace_id": workspace_id, "user_id": user_id}, {"$set": {"workspace_id": workspace_id, "user_id": user_id, "enabled": enabled, "at": _now()}}, upsert=True)


async def is_enabled(workspace_id: str, user_id: str) -> bool:
    row = await prefs().find_one({"workspace_id": workspace_id, "user_id": user_id}, {"enabled": 1})
    return bool(row and row.get("enabled"))


async def waiting_drafts(workspace_id: str) -> list[dict[str, Any]]:
    """Drafts waiting for approval, newest first. A post the safety check held is not offered a one-tap approve."""
    rows = await get_db()["content_pieces"].find(
        {"workspace_id": workspace_id, "approval_status": "pending", "deleted": {"$ne": True}, "archived": {"$ne": True}, "flagged_for_review": {"$ne": True}}
    ).sort("created_at", -1).limit(MAX_ITEMS + 1).to_list(length=MAX_ITEMS + 1)
    return rows


async def build(workspace_id: str, user_id: str, role: str) -> dict[str, Any] | None:
    """What to put in this person's digest, or None when nothing is waiting for them."""
    held = ROLE_PERMISSIONS.get(role, set())
    if "approve_content" not in held:
        return None
    drafts = await waiting_drafts(workspace_id)
    requests = await ledger.approval_requests().find({"workspace_id": workspace_id, "status": "pending"}).sort("created_at", -1).limit(MAX_ITEMS).to_list(length=MAX_ITEMS)
    if not drafts and not requests:
        return None
    items: list[dict[str, str]] = []
    for d in drafts[:MAX_ITEMS]:
        items.append({
            "kind": "draft", "title": f"{d.get('platform') or 'Post'}: {str(d.get('content') or '')[:90]}",
            "approve_url": await links.make(workspace_id, user_id, "approve_post", d["piece_id"]),
            "send_back_url": await links.make(workspace_id, user_id, "send_back", d["piece_id"]),
        })
    if "publish_content" in held:
        for r in requests:
            items.append({
                "kind": "approval", "title": f"A connection wants to {str((r.get('preview') or {}).get('action') or r['tool']).rstrip('.').lower()}",
                "approve_url": await links.make(workspace_id, user_id, "yes_send", r["id"]), "send_back_url": await links.make(workspace_id, user_id, "no_send", r["id"]),
            })
    if not items:
        return None
    return {"count": len(items), "more": len(drafts) > MAX_ITEMS, "items": items}


def template_variables(content: dict[str, Any]) -> dict[str, Any]:
    """The digest as plain text variables, the way the other templates take them (a Resend template has no loop, so the waiting items are one text block).
    Each item is a title and its two links, one under the other."""
    from app.core.config import settings

    blocks = [f"{n}. {item['title']}\nApprove: {item['approve_url']}\nSend back: {item['send_back_url']}" for n, item in enumerate(content["items"], start=1)]
    return {
        "count": str(content["count"]),
        "list": "\n\n".join(blocks),
        "more_note": "There are more waiting in Ekavo." if content["more"] else "",
        "open_url": f"{settings.FRONTEND_URL.rstrip('/')}/dashboard",
    }


async def send_for(workspace_id: str, user_id: str, role: str, email: str) -> bool:
    content = await build(workspace_id, user_id, role)
    if content is None:
        return False
    return await send_templated_email("approvals-digest", email, template_variables(content))


@distributed_job_lock("tool_digest", ttl_seconds=1800)
async def send_digests() -> int:
    """Called once a morning. Sends each opted-in person their digest, at most one a day; returns how many were sent."""
    sent = 0
    day = _now().date().isoformat()
    async for pref in prefs().find({"enabled": True}).limit(MAX_PEOPLE):
        try:
            if pref.get("last_sent") == day:
                continue
            member = await get_db()["workspace_members"].find_one({"workspace_id": pref["workspace_id"], "user_id": pref["user_id"], "status": "active"}, {"role": 1})
            user = await get_db()["users"].find_one({"id": pref["user_id"]}, {"email": 1})
            if not member or not user or not user.get("email"):
                continue
            claimed = await prefs().find_one_and_update({"_id": pref["_id"], "last_sent": {"$ne": day}}, {"$set": {"last_sent": day}})
            if claimed is None:
                continue
            if await send_for(pref["workspace_id"], pref["user_id"], member["role"], user["email"]):
                sent += 1
        except Exception as exc:  # noqa: BLE001 - one person's problem never stops the others
            logger.warning("Could not send a digest: %s", exc)
    return sent
