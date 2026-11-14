"""Conversations with Ask Ekavo: a thread, its ordered messages, a short running summary, and who may read it.

One conversation core for every surface (the app, WhatsApp, Telegram, later the command line and assistants). Built so a workspace group chat can be added later
without moving anything: a thread has a list of participants and a visibility (private today), every message records its author (a member or Ekavo) and its
surface, and messages are an ordered, append-only log with a sequence number.

Rules held from the start:
* a private thread is read only by its owner; a thread in another workspace is never found (the same answer as "not found");
* a message is never edited, only added;
* Ekavo acts as the member who asked, never as the group (see `runs.py`);
* threads are kept for `KEEP_DAYS` after the last message and then removed with their messages (`cleanup`)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from pymongo import ReturnDocument

from app.ask import fence
from app.db.mongo import get_db

KEEP_DAYS = 30
MAX_TITLE = 60
MAX_TEXT = 2000
RECENT = 6                      # messages kept word for word in the context
FOLD_AT = 8                     # older messages not yet in the summary before it is rolled forward
SUMMARY_CHARS = 600
VISIBILITIES = ("private", "workspace", "group")
KINDS = ("text", "voice", "link", "plan", "result", "answer", "question", "note")
SURFACES = ("app", "whatsapp", "telegram", "cli", "mcp", "api", "simulator")


class ThreadError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def threads():
    return get_db()["ask_threads"]


def messages():
    return get_db()["ask_messages"]


async def create_indexes() -> None:
    await threads().create_index("id", unique=True)
    await threads().create_index([("workspace_id", 1), ("owner_id", 1), ("updated_at", -1)])
    await threads().create_index([("workspace_id", 1), ("link_key", 1)], sparse=True)
    await messages().create_index([("thread_id", 1), ("seq", 1)], unique=True)


def author_member(user_id: str) -> dict[str, str]:
    return {"type": "member", "id": user_id}


EKAVO = {"type": "ekavo", "id": "ekavo"}


def _title(text: str) -> str:
    one = re.sub(r"\s+", " ", fence.clean(text or "", 200)).strip()
    return (one[: MAX_TITLE - 1] + "…") if len(one) > MAX_TITLE else one


async def create(workspace_id: str, owner_id: str, surface: str = "app", *, title: str = "", link_key: Optional[str] = None) -> dict[str, Any]:
    """A new, empty, private thread. `link_key` ties a chat-app contact to its thread (see `for_contact`)."""
    if surface not in SURFACES:
        raise ThreadError("input_invalid", "Unknown place for a conversation.", 422)
    now = _now()
    doc = {
        "id": uuid4().hex, "workspace_id": workspace_id, "owner_id": owner_id, "participants": [owner_id], "visibility": "private", "surface": surface,
        "title": _title(title), "summary": "", "summary_upto": 0, "last_seq": 0, "message_count": 0, "created_at": now, "updated_at": now,
    }
    if link_key:
        doc["link_key"] = link_key
    await threads().insert_one(dict(doc))
    return public(doc)


def public(doc: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "workspace_id", "participants", "visibility", "surface", "title", "message_count", "last_seq")
    out = {k: doc.get(k) for k in keys}
    out["created_at"] = _iso(doc.get("created_at"))
    out["updated_at"] = _iso(doc.get("updated_at"))
    return out


def can_read(doc: dict[str, Any], workspace_id: str, user_id: str) -> bool:
    """Who may read a thread: nobody outside its workspace; the owner of a private one; any member of the workspace for a workspace one; a participant for a group one."""
    if doc.get("workspace_id") != workspace_id:
        return False
    visibility = doc.get("visibility", "private")
    if visibility == "workspace":
        return True
    if visibility == "group":
        return user_id in (doc.get("participants") or [])
    return doc.get("owner_id") == user_id


async def load(workspace_id: str, user_id: str, thread_id: str) -> dict[str, Any]:
    """The thread, or "not found" for one that is missing, in another workspace, or not the person's to read (the same answer for all three)."""
    doc = await threads().find_one({"id": thread_id}, {"_id": 0})
    if doc is None or not can_read(doc, workspace_id, user_id):
        raise ThreadError("not_found", "That conversation was not found. Conversations are kept for 30 days.", 404)
    return doc


async def append(thread: dict[str, Any], author: dict[str, str], text: str, *, kind: str = "text", surface: str = "app", run_id: Optional[str] = None,
                 refs: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Add one message at the next sequence number. The number is taken atomically, so two messages at once never share one."""
    if kind not in KINDS:
        raise ThreadError("input_invalid", "Unknown message kind.", 422)
    body = fence.clean(text or "", MAX_TEXT)
    now = _now()
    bumped = await threads().find_one_and_update(
        {"id": thread["id"]}, {"$inc": {"last_seq": 1, "message_count": 1}, "$set": {"updated_at": now}}, return_document=ReturnDocument.AFTER, projection={"last_seq": 1, "title": 1})
    if bumped is None:
        raise ThreadError("not_found", "That conversation was not found.", 404)
    doc = {"thread_id": thread["id"], "workspace_id": thread["workspace_id"], "seq": bumped["last_seq"], "author": author, "surface": surface, "kind": kind, "text": body,
           "run_id": run_id, "refs": refs or {}, "created_at": now}
    await messages().insert_one(dict(doc))
    if not bumped.get("title") and author.get("type") == "member" and body:
        await threads().update_one({"id": thread["id"], "title": ""}, {"$set": {"title": _title(body)}})
    return view(doc)


def view(doc: dict[str, Any]) -> dict[str, Any]:
    return {"seq": doc["seq"], "author": doc["author"], "surface": doc.get("surface"), "kind": doc.get("kind"), "text": doc.get("text", ""), "run_id": doc.get("run_id"),
            "refs": doc.get("refs") or {}, "created_at": _iso(doc.get("created_at"))}


async def page(thread_id: str, *, after: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    rows = await messages().find({"thread_id": thread_id, "seq": {"$gt": after}}, {"_id": 0}).sort("seq", 1).limit(max(1, min(limit, 100))).to_list(length=100)
    return [view(r) for r in rows]


async def recent(thread_id: str, n: int = RECENT) -> list[dict[str, Any]]:
    rows = await messages().find({"thread_id": thread_id}, {"_id": 0}).sort("seq", -1).limit(n).to_list(length=n)
    return [view(r) for r in reversed(rows)]


async def listing(workspace_id: str, user_id: str, *, q: str = "", limit: int = 20, before: Optional[datetime] = None) -> dict[str, Any]:
    """The person's own conversations, newest first. A search matches the title (the words are escaped, so it is never read as a pattern)."""
    query: dict[str, Any] = {"workspace_id": workspace_id, "$or": [{"owner_id": user_id, "visibility": "private"}, {"visibility": "workspace"}, {"visibility": "group", "participants": user_id}]}
    if q.strip():
        query["title"] = {"$regex": re.escape(q.strip()[:80]), "$options": "i"}
    if before is not None:
        query["updated_at"] = {"$lt": before}
    limit = max(1, min(limit, 50))
    rows = await threads().find(query, {"_id": 0}).sort("updated_at", -1).limit(limit + 1).to_list(length=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [public(r) for r in rows], "next_before": _iso(rows[-1]["updated_at"]) if more and rows else None}


async def remove(workspace_id: str, user_id: str, thread_id: str) -> None:
    """Only the owner removes a conversation, with its messages."""
    doc = await load(workspace_id, user_id, thread_id)
    if doc.get("owner_id") != user_id:
        raise ThreadError("not_found", "That conversation was not found.", 404)
    await messages().delete_many({"thread_id": thread_id})
    await threads().delete_one({"id": thread_id})


async def for_contact(workspace_id: str, owner_id: str, surface: str, contact_key: str, *, idle_hours: int = 12) -> dict[str, Any]:
    """The conversation with one chat-app contact: the open one, or a new one when there is none yet or it has been quiet for `idle_hours` (or on `start_new`)."""
    key = f"{surface}:{contact_key}"
    doc = await threads().find_one({"workspace_id": workspace_id, "link_key": key, "owner_id": owner_id}, {"_id": 0}, sort=[("updated_at", -1)])
    if doc is not None:
        updated = doc["updated_at"] if doc["updated_at"].tzinfo else doc["updated_at"].replace(tzinfo=timezone.utc)
        if _now() - updated < timedelta(hours=idle_hours):
            return doc
    made = await create(workspace_id, owner_id, surface, link_key=key)
    return await threads().find_one({"id": made["id"]}, {"_id": 0}) or made


async def start_new(workspace_id: str, owner_id: str, surface: str, contact_key: str) -> dict[str, Any]:
    """A fresh conversation for a contact (the person sent NEW). The old one stays until it expires."""
    made = await create(workspace_id, owner_id, surface, link_key=f"{surface}:{contact_key}")
    return await threads().find_one({"id": made["id"]}, {"_id": 0}) or made


async def add_pending_ref(thread_id: str, ref_id: str, keep: int = 3) -> None:
    """A picture a chat contact sent, waiting for the request it is for. The newest `keep` are kept."""
    await threads().update_one({"id": thread_id}, {"$push": {"pending_refs": {"$each": [ref_id], "$slice": -keep}}})


async def take_pending_refs(thread_id: str) -> list[str]:
    """The waiting pictures, handed over once: they belong to the next request and are cleared."""
    doc = await threads().find_one_and_update({"id": thread_id}, {"$set": {"pending_refs": []}}, projection={"pending_refs": 1})
    return [str(i) for i in ((doc or {}).get("pending_refs") or [])]


# ── the running summary ───────────────────────────────────────────────────────

def needs_summary(thread: dict[str, Any]) -> bool:
    """True when enough older messages have piled up outside the recent window to fold into the summary."""
    return (int(thread.get("last_seq") or 0) - RECENT) - int(thread.get("summary_upto") or 0) >= FOLD_AT


async def roll_summary(thread_id: str, think: Any) -> bool:
    """Fold the older messages into the running summary with the small model (free). Returns whether it changed. Any failure keeps the old summary."""
    doc = await threads().find_one({"id": thread_id}, {"_id": 0})
    if doc is None or not needs_summary(doc):
        return False
    upto = int(doc["last_seq"]) - RECENT
    older = await messages().find({"thread_id": thread_id, "seq": {"$gt": int(doc.get("summary_upto") or 0), "$lte": upto}}, {"_id": 0}).sort("seq", 1).to_list(length=200)
    lines = [f"{'Person' if m['author'].get('type') == 'member' else 'Ekavo'}: {fence.clean(m.get('text', ''), 300)}" for m in older]
    prompt = ("Update the short summary of this conversation. Keep names, platforms, topics and decisions. At most 600 characters. Return JSON {\"summary\": \"...\"}.\n\n"
              + fence.fence(f"Summary so far: {doc.get('summary') or '(none)'}\n" + "\n".join(lines), 4000))
    try:
        raw = await think("respond", prompt, fence.RULES)
        text = fence.clean(str(raw.get("summary") or ""), SUMMARY_CHARS)
    except Exception:  # noqa: BLE001 - the old summary is always safe
        return False
    if not text:
        return False
    await threads().update_one({"id": thread_id}, {"$set": {"summary": text, "summary_upto": upto}})
    return True


async def cleanup() -> int:
    """Remove conversations quiet for `KEEP_DAYS`, with their messages. Returns how many threads went."""
    cutoff = _now() - timedelta(days=KEEP_DAYS)
    old = [d["id"] async for d in threads().find({"updated_at": {"$lt": cutoff}}, {"id": 1})]
    if old:
        await messages().delete_many({"thread_id": {"$in": old}})
        await threads().delete_many({"id": {"$in": old}})
    return len(old)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None
