"""A record of each time a person used a data right: a copy of their data, or deleting their account.

Each row says what was asked, when it was asked and when it was done. It holds no name, email or account id: the person is a short
one-way digest, made the same way as in `account_erasures`, so the record can be kept after the account is gone and still be matched
when the same person asks about it later.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.db.mongo import get_db

logger = logging.getLogger(__name__)

KINDS = ("export", "erase")
MAX_LISTED = 500


def requests_col():
    return get_db()["data_requests"]


def who_digest(user: dict[str, Any]) -> str:
    return hashlib.sha256(str(user.get("email") or user.get("id") or "").lower().encode()).hexdigest()[:16]


async def record(kind: str, user: dict[str, Any]) -> None:
    """Write down that this request was carried out. A failure here is logged and never stops the person's own request."""
    if kind not in KINDS:
        raise ValueError(f"unknown data request kind: {kind}")
    now = datetime.now(timezone.utc)
    try:
        await requests_col().insert_one(
            {"id": uuid4().hex, "kind": kind, "who": who_digest(user), "asked_at": now, "done_at": now, "status": "done"}
        )
    except Exception as exc:  # noqa: BLE001 - the member's request already succeeded
        logger.error("data request (%s) could not be recorded: %s", kind, exc)


async def listing(limit: int = 100) -> list[dict[str, Any]]:
    """The newest requests first, for staff."""
    rows = requests_col().find({}, {"_id": 0}).sort("asked_at", -1).limit(max(1, min(limit, MAX_LISTED)))
    return [row async for row in rows]


async def create_indexes() -> None:
    # the staff list reads newest first
    await requests_col().create_index([("asked_at", -1)])
