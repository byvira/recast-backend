"""The ledger of tool calls, and the records the gatekeeper keeps for repeated and approved calls.

`tool_calls` has one row for every call that reached the gatekeeper, allowed or refused, kept 90 days. A row names the surface, the key or
session, the member, the tool and the outcome; it holds which fields were sent and never what they held. `tool_idempotency` makes a repeated
request with the same key answer the same, once. `tool_approvals` holds a person's yes for one publish call, once, for a short time."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_db

LEDGER_DAYS = 90
IDEMPOTENCY_HOURS = 24
APPROVAL_MINUTES = 15
REQUEST_HOURS = 48
REQUEST_HOURS = 48
RATE_WINDOW_SECONDS = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def calls():
    return get_db()["tool_calls"]


def idempotency():
    return get_db()["tool_idempotency"]


def approvals():
    return get_db()["tool_approvals"]


def approval_requests():
    return get_db()["tool_approval_requests"]


def approval_requests():
    return get_db()["tool_approval_requests"]


def rates():
    return get_db()["tool_rate"]


def settings():
    return get_db()["tool_settings"]


async def create_indexes() -> None:
    await calls().create_index("at", expireAfterSeconds=LEDGER_DAYS * 24 * 3600)
    await calls().create_index([("workspace_id", 1), ("at", -1)])
    await calls().create_index([("credential_id", 1), ("at", -1)])
    await idempotency().create_index("at", expireAfterSeconds=IDEMPOTENCY_HOURS * 3600)
    await approvals().create_index("expires_at", expireAfterSeconds=0)
    await approvals().create_index("id", unique=True)
    await approval_requests().create_index("id", unique=True)
    await approval_requests().create_index([("workspace_id", 1), ("status", 1), ("created_at", -1)])
    await approval_requests().create_index("created_at", expireAfterSeconds=REQUEST_HOURS * 3600)
    await approval_requests().create_index("id", unique=True)
    await approval_requests().create_index([("workspace_id", 1), ("status", 1), ("created_at", -1)])
    await approval_requests().create_index("created_at", expireAfterSeconds=REQUEST_HOURS * 3600)
    await rates().create_index("at", expireAfterSeconds=RATE_WINDOW_SECONDS)
    await settings().create_index([("scope", 1), ("workspace_id", 1), ("tool", 1)])


# ── the ledger ───────────────────────────────────────────────────────────

async def record(entry: dict[str, Any]) -> str:
    row = {"id": uuid4().hex, **entry}
    row.setdefault("at", _now())
    await calls().insert_one(dict(row))
    return row["id"]


async def finish(call_id: str, *, outcome: str, charged_credits: int = 0, code: Optional[str] = None) -> None:
    """Say how an admitted call ended: done or failed, and what it really cost."""
    await calls().update_one({"id": call_id}, {"$set": {"outcome": outcome, "charged_credits": int(charged_credits), "code": code, "ended_at": _now()}})


def month_start(when: Optional[datetime] = None) -> datetime:
    when = when or _now()
    return when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def spent_this_month(credential_id: str) -> int:
    """Credits this key has used in the calendar month: what finished calls really cost, plus what admitted calls are still holding."""
    rows = await calls().aggregate([
        {"$match": {"credential_id": credential_id, "at": {"$gte": month_start()}, "outcome": {"$in": ["done", "admitted"]}}},
        {"$group": {"_id": None, "credits": {"$sum": {"$ifNull": ["$charged_credits", {"$ifNull": ["$estimate_credits", 0]}]}}}},
    ]).to_list(length=1)
    return int(rows[0]["credits"]) if rows else 0


# ── repeated requests ────────────────────────────────────────────────────

def _idem_id(credential_id: str, tool: str, key: str) -> str:
    return hashlib.sha256(f"{credential_id}|{tool}|{key}".encode("utf-8")).hexdigest()


async def earlier_answer(credential_id: str, tool: str, key: str, fingerprint: str) -> tuple[str, Optional[dict[str, Any]]]:
    """A new key is claimed and answers ("none", None); a repeat of a finished request answers ("replay", what it answered); a repeat that is
    still running answers ("in_progress", None); the same key with different input answers ("conflict", None)."""
    doc_id = _idem_id(credential_id, tool, key)
    try:
        await idempotency().insert_one({"_id": doc_id, "credential_id": credential_id, "tool": tool, "fingerprint": fingerprint, "state": "pending", "at": _now()})
        return "none", None
    except DuplicateKeyError:
        pass
    found = await idempotency().find_one({"_id": doc_id})
    if not found:
        return await earlier_answer(credential_id, tool, key, fingerprint)
    if found["fingerprint"] != fingerprint:
        return "conflict", None
    if found["state"] == "done":
        return "replay", found.get("result") or {}
    return "in_progress", None


async def complete(credential_id: str, tool: str, key: str, result: dict[str, Any]) -> None:
    await idempotency().update_one({"_id": _idem_id(credential_id, tool, key)}, {"$set": {"state": "done", "result": result, "done_at": _now()}})


async def abandon(credential_id: str, tool: str, key: str) -> None:
    """The call failed before it made anything, so the same key may be tried again."""
    await idempotency().delete_one({"_id": _idem_id(credential_id, tool, key), "state": "pending"})


# ── a person's yes to one publish call ───────────────────────────────────

async def issue_approval(workspace_id: str, user_id: str, tool: str, fingerprint: str) -> str:
    """Made when a person answers yes in Ekavo. It works once, for this tool and this exact input, for a few minutes."""
    token = uuid4().hex + uuid4().hex
    await approvals().insert_one({
        "id": token, "workspace_id": workspace_id, "user_id": user_id, "tool": tool, "fingerprint": fingerprint, "used": False,
        "expires_at": _now() + timedelta(minutes=APPROVAL_MINUTES),
    })
    return token


async def use_approval(workspace_id: str, tool: str, fingerprint: str, token: Optional[str]) -> bool:
    if not token:
        return False
    taken = await approvals().find_one_and_update(
        {"id": token, "workspace_id": workspace_id, "tool": tool, "fingerprint": fingerprint, "used": False, "expires_at": {"$gt": _now()}},
        {"$set": {"used": True, "used_at": _now()}},
    )
    return taken is not None


# ── rate counters ────────────────────────────────────────────────────────

async def hit(credential_id: str) -> int:
    """Count one call for this key in the current minute and return how many that makes."""
    minute = _now().strftime("%Y%m%d%H%M")
    row = await rates().find_one_and_update(
        {"_id": f"{credential_id}:{minute}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": _now()}}, upsert=True, return_document=True,
    )
    return int(row["n"])
