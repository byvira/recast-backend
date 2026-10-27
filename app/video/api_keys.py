"""Workspace API keys for the public video API. A key is shown once, when it is made; only a hash is stored. Each key carries the
scopes it may use (read or render) and belongs to one workspace. Keys are made, listed and revoked by an owner or admin."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db

PREFIX = "ek_live_"
SCOPES = ("video:read", "video:render")
MAX_KEYS = 10
TOUCH_EVERY = timedelta(minutes=10)


class KeyError_(ValueError):
    """A refusal with a plain message."""


def _keys():
    return get_db()["api_keys"]


def _hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    if isinstance(v, datetime):
        v = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return v.isoformat().replace("+00:00", "Z")
    return None


def public(doc: dict) -> dict[str, Any]:
    return {"id": doc["id"], "name": doc["name"], "prefix": doc["prefix"], "scopes": doc["scopes"], "created_at": _iso(doc.get("created_at")),
            "last_used_at": _iso(doc.get("last_used_at")), "revoked": bool(doc.get("revoked"))}


async def create(workspace_id: str, user_id: str, name: str, scopes: list[str]) -> tuple[dict[str, Any], str]:
    """Make a key. Returns (its public record, the key itself, shown once)."""
    name = (name or "").strip()[:60]
    if not name:
        raise KeyError_("Give the key a name so you can tell it apart later.")
    chosen = [s for s in dict.fromkeys(scopes or []) if s in SCOPES]
    if not chosen:
        raise KeyError_("Choose what the key may do: read your videos, or also start renders.")
    from app.shared import entitlements

    await entitlements.require_room(
        await entitlements.entitlements_for(workspace_id), "api_keys",
        legacy_message=f"A workspace can have {MAX_KEYS} keys. Revoke one you no longer use first.",
    )
    plain = PREFIX + secrets.token_urlsafe(30)
    doc = {"id": uuid4().hex, "workspace_id": workspace_id, "name": name, "prefix": plain[: len(PREFIX) + 6], "hash": _hash(plain), "scopes": chosen,
           "created_by": user_id, "created_at": _now(), "last_used_at": None, "revoked": False}
    await _keys().insert_one(dict(doc))
    return public(doc), plain


async def list_keys(workspace_id: str) -> list[dict[str, Any]]:
    rows = await _keys().find({"workspace_id": workspace_id}).sort("created_at", -1).to_list(length=50)
    return [public(r) for r in rows]


async def revoke(workspace_id: str, key_id: str) -> bool:
    res = await _keys().update_one({"id": key_id, "workspace_id": workspace_id}, {"$set": {"revoked": True, "revoked_at": _now()}})
    return bool(res.matched_count)


async def authenticate(plain: str) -> Optional[dict]:
    """The key's record for a key that is right and not revoked, or None. Last use is written at most every ten minutes."""
    if not plain.startswith(PREFIX) or len(plain) > 200:
        return None
    doc = await _keys().find_one({"hash": _hash(plain), "revoked": {"$ne": True}})
    if not doc:
        return None
    last = doc.get("last_used_at")
    if not last or _now() - (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) > TOUCH_EVERY:
        await _keys().update_one({"id": doc["id"]}, {"$set": {"last_used_at": _now()}})
    return doc
