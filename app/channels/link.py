"""Linking a chat to a member.

The member asks for a code in the app (Settings, Chat apps). The code is eight characters from an alphabet with nothing easily mixed up, shown as
ABCD-2345, kept only as a hash, good for 15 minutes and for one use. The person sends `LINK ABCD-2345` from the chat. From then on that chat acts as
that member, with their role, through the same gatekeeper as the app.

The chat's own id (a phone number, a Telegram id) is never stored as it is: it is kept as a keyed hash plus its last four characters, so the list
in the app can say "chat ending 4821" and a leak of the table does not hand out phone numbers. Unlinking is one tap in the app, or the word STOP."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.core.config import settings
from app.db.mongo import get_db

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"        # no 0, O, 1, I or L
CODE_LENGTH = 8
CODE_MINUTES = 15
MAX_LINKS_PER_MEMBER = 3
MAX_LINKS_PER_WORKSPACE = 25
MAX_WRONG_CODES_PER_HOUR = 5
CHANNELS = ("whatsapp", "telegram", "simulator")
_LINK = re.compile(r"^\s*link\s+([A-Za-z0-9\- ]{8,12})\s*$", re.IGNORECASE)


class LinkError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def codes():
    return get_db()["channel_codes"]


def links():
    return get_db()["channel_links"]


def attempts():
    return get_db()["channel_attempts"]


async def create_indexes() -> None:
    await codes().create_index("hash", unique=True)
    await codes().create_index("expires_at", expireAfterSeconds=0)
    await links().create_index([("channel", 1), ("sender_hash", 1)], unique=True)
    await links().create_index([("workspace_id", 1), ("user_id", 1)])
    await attempts().create_index("at", expireAfterSeconds=3600)


def sender_hash(channel: str, sender: str) -> str:
    """A keyed hash of the chat's id, so the table holds no phone numbers."""
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), f"{channel}:{sender}".encode("utf-8"), hashlib.sha256).hexdigest()


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.upper().replace("-", "").replace(" ", "").encode("utf-8")).hexdigest()


def pretty(raw: str) -> str:
    return f"{raw[:4]}-{raw[4:]}"


def parse_link_message(text: str) -> Optional[str]:
    """The code in a `LINK ABCD-2345` message, or None when the message is not that."""
    match = _LINK.match(text or "")
    if not match:
        return None
    code = match.group(1).upper().replace("-", "").replace(" ", "")
    return code if len(code) == CODE_LENGTH and all(c in ALPHABET for c in code) else None


async def make_code(workspace_id: str, user_id: str, channel: str) -> dict[str, Any]:
    """A new one-time code for this member, shown once in the app."""
    if channel not in CHANNELS:
        raise LinkError("input_invalid", "Choose WhatsApp or Telegram.", 422)
    if await links().count_documents({"workspace_id": workspace_id, "user_id": user_id, "status": "active"}) >= MAX_LINKS_PER_MEMBER:
        raise LinkError("too_many", f"You can link {MAX_LINKS_PER_MEMBER} chats. Unlink one first.", 409)
    if await links().count_documents({"workspace_id": workspace_id, "status": "active"}) >= MAX_LINKS_PER_WORKSPACE:
        raise LinkError("workspace_full", f"A workspace can link {MAX_LINKS_PER_WORKSPACE} chats. Unlink one first.", 409)
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    expires = _now() + timedelta(minutes=CODE_MINUTES)
    await codes().delete_many({"workspace_id": workspace_id, "user_id": user_id, "channel": channel})          # one live code at a time
    await codes().insert_one({"hash": _code_hash(raw), "workspace_id": workspace_id, "user_id": user_id, "channel": channel, "expires_at": expires, "made_at": _now()})
    return {"code": pretty(raw), "channel": channel, "expires_at": expires.isoformat().replace("+00:00", "Z"), "message": f"LINK {pretty(raw)}"}


async def redeem(channel: str, sender: str, raw_code: str, services: Any) -> Optional[dict[str, Any]]:
    """Link this chat when the code is right. Returns the link, or None (the person is told only that it did not work)."""
    key = sender_hash(channel, sender)
    if await attempts().count_documents({"key": key}) >= MAX_WRONG_CODES_PER_HOUR:
        return None
    row = await codes().find_one_and_delete({"hash": _code_hash(raw_code), "channel": channel, "expires_at": {"$gt": _now()}})      # one use, even if two arrive at once
    if row is None:
        await attempts().insert_one({"key": key, "at": _now()})
        return None
    permissions = await services.member_permissions(row["workspace_id"], row["user_id"])
    if permissions is None or "create_content" not in permissions:
        return None
    doc = {"id": uuid4().hex, "channel": channel, "sender_hash": key, "sender_tail": str(sender)[-4:], "workspace_id": row["workspace_id"], "user_id": row["user_id"],
           "status": "active", "created_at": _now(), "last_inbound_at": _now(), "language": None}
    await links().delete_many({"channel": channel, "sender_hash": key})                  # linking again replaces the old link
    await links().insert_one(dict(doc))
    return doc


async def find(channel: str, sender: str) -> Optional[dict[str, Any]]:
    return await links().find_one({"channel": channel, "sender_hash": sender_hash(channel, sender), "status": "active"}, {"_id": 0})


async def touch(link_id: str, language: Optional[str] = None) -> None:
    change: dict[str, Any] = {"last_inbound_at": _now()}
    if language:
        change["language"] = language
    await links().update_one({"id": link_id}, {"$set": change})


async def list_for(workspace_id: str, user_id: str) -> list[dict[str, Any]]:
    rows = await links().find({"workspace_id": workspace_id, "user_id": user_id, "status": "active"}, {"_id": 0, "sender_hash": 0}).sort("created_at", -1).to_list(length=20)
    return [{**r, "created_at": _iso(r.get("created_at")), "last_inbound_at": _iso(r.get("last_inbound_at"))} for r in rows]


async def unlink(link_id: str, workspace_id: str, user_id: str) -> bool:
    done = await links().delete_one({"id": link_id, "workspace_id": workspace_id, "user_id": user_id})
    return done.deleted_count == 1


async def unlink_sender(channel: str, sender: str) -> bool:
    done = await links().delete_one({"channel": channel, "sender_hash": sender_hash(channel, sender)})
    return done.deleted_count == 1


def _iso(value: Any) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None
