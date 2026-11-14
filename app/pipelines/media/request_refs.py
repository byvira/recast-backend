"""Pictures a person sends with one request, so the pictures made for it follow their look ("make posts like these").

Free and private by design: a picture is read once, with no model and no cost, into a few numbers about its look (brightness, colour, warmth, detail, main colours: the
same reading the brand's own reference pictures get, see `reference_style.py`). The picture itself is NOT kept; only that reading is, for 24 hours, tied to the person who
sent it. While a request runs, the look is added to every picture prompt made in that request (`active_line`, read by `reference_style.with_style_line`).

Limits: three pictures a request, 8 MB each, ten a day for each person. Pictures made later by a background job (a campaign batch started from a request) do not see
the look yet; that needs the look carried in the job's payload. An image-to-image model is not used: the free options are loose copies at best, so it stays off until one
live check shows it is worth it (see docs/plans/v2_plan.md, L26)."""
from __future__ import annotations

import contextvars
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db
from app.pipelines.media import reference_style

MAX_PER_REQUEST = 3
MAX_BYTES = 8 * 1024 * 1024
PER_DAY = 10
KEEP_HOURS = 24

#: the look to add to every picture prompt made while a request runs; empty outside one
CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar("request_style_line", default="")


class RefError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def col():
    return get_db()["request_references"]


async def create_indexes() -> None:
    await col().create_index("id", unique=True)
    await col().create_index("created_at", expireAfterSeconds=KEEP_HOURS * 3600)
    await col().create_index([("workspace_id", 1), ("user_id", 1), ("created_at", -1)])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def add(workspace_id: str, user_id: str, data: bytes) -> dict[str, Any]:
    """Read one picture and keep only its look. Raises `RefError` with a plain message for a file that is empty, too big, not a picture or over the daily limit."""
    if not data:
        raise RefError("input_invalid", "Nothing was sent.", 422)
    if len(data) > MAX_BYTES:
        raise RefError("too_large", "That picture is too large. Keep it under 8 MB.", 413)
    if await col().count_documents({"workspace_id": workspace_id, "user_id": user_id, "created_at": {"$gte": _now() - timedelta(days=1)}}) >= PER_DAY:
        raise RefError("limit", f"You can send {PER_DAY} reference pictures a day. Try again tomorrow.", 429)
    read = reference_style.read_one(data)
    if read is None:
        raise RefError("unreadable", "That does not look like a picture I can read. Try a JPG or PNG.", 422)
    ident = uuid4().hex
    await col().insert_one({"id": ident, "workspace_id": workspace_id, "user_id": user_id, "read": read, "created_at": _now()})
    made = reference_style.describe([read])
    return {"id": ident, "traits": made["traits"], "colours": made["colours"]}


async def line_for(workspace_id: str, user_id: str, ids: list[str]) -> str:
    """The sentence describing the look these pictures share, or an empty string when none of them is the person's or any is gone (they last 24 hours)."""
    clean = [str(i) for i in dict.fromkeys(ids or [])][:MAX_PER_REQUEST]
    if not clean:
        return ""
    docs = await col().find({"id": {"$in": clean}, "workspace_id": workspace_id, "user_id": user_id}, {"_id": 0, "read": 1}).to_list(length=MAX_PER_REQUEST)
    made = reference_style.describe([d["read"] for d in docs if d.get("read")])
    return made["line"].replace("the brand's own pictures", "the pictures the person sent for this request") if made["line"] else ""


def active_line() -> str:
    return CURRENT.get()


def check_ids(ids: Optional[list[str]]) -> list[str]:
    """At most three distinct ids, as plain strings."""
    return [str(i)[:64] for i in dict.fromkeys(ids or [])][:MAX_PER_REQUEST]
