"""Website content as drafts and live copies. A draft is saved as staff type; the website only ever reads the live copy. Going
live (now or at a set time) copies the draft, adds a version, and keeps every earlier version so one can be put back."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from app.db.mongo import get_db
from app.site_content import rules as R


def content_col():
    return get_db()["site_content"]


def versions_col():
    return get_db()["site_content_versions"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def by_of(staff: dict[str, Any]) -> dict[str, Any]:
    return {"id": staff.get("id"), "name": staff.get("name") or staff.get("email") or "Someone"}


def status_of(doc: dict[str, Any]) -> str:
    if doc.get("schedule_at"):
        return "scheduled"
    if doc.get("review"):
        return "review"
    if doc.get("live") is not None and doc.get("live") == doc.get("draft"):
        return "live"
    return "draft"


def row(doc: dict[str, Any]) -> dict[str, Any]:
    """One line of the Ops list."""
    draft = doc.get("draft") or {}
    return {
        "id": doc["id"], "kind": doc["kind"], "key": doc["key"], "title": R.title_of(doc["kind"], doc["key"], draft),
        "status": status_of(doc), "has_live": doc.get("live") is not None, "updated_at": doc.get("updated_at"),
        "updated_by": (doc.get("updated_by") or {}).get("name"), "schedule_at": doc.get("schedule_at"), "live_at": doc.get("live_at"),
        "review_by": (doc.get("review") or {}).get("by_name"),
        "closes_on": draft.get("closes_on") if doc["kind"] == "job" else None,
    }


def full(doc: dict[str, Any]) -> dict[str, Any]:
    """Everything the editor needs."""
    out = {k: doc.get(k) for k in ("id", "kind", "key", "draft", "draft_version", "live", "live_version", "live_at", "schedule_at",
                                    "review", "sent_back", "created_at", "updated_at")}
    out["updated_by"] = (doc.get("updated_by") or {}).get("name")
    out["live_by"] = (doc.get("live_by") or {}).get("name")
    out["scheduled_by"] = (doc.get("scheduled_by") or {}).get("name")
    out["status"] = status_of(doc)
    out["problems"] = R.problems(doc["kind"], doc["key"], doc.get("draft") or {})
    out["title"] = R.title_of(doc["kind"], doc["key"], doc.get("draft") or {})
    return out


async def get(content_id: str) -> dict[str, Any]:
    doc = await content_col().find_one({"id": content_id[:60], "deleted": {"$ne": True}}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="That page or post doesn't exist any more.")
    return doc


async def page_doc(key: str) -> Optional[dict[str, Any]]:
    return await content_col().find_one({"kind": "page", "key": key, "deleted": {"$ne": True}}, {"_id": 0})


async def _slug_free(kind: str, slug: str, content_id: Optional[str]) -> bool:
    q: dict[str, Any] = {"kind": kind, "key": slug, "deleted": {"$ne": True}}
    if content_id:
        q["id"] = {"$ne": content_id}
    return not await content_col().find_one(q, {"_id": 1})


async def create(kind: str, key: Optional[str], data: dict[str, Any], staff: dict[str, Any]) -> dict[str, Any]:
    """A new post or job (key from its slug), or the first draft of a page (key is the page)."""
    if kind == "page":
        if key not in R.PAGES:
            raise HTTPException(status_code=404, detail="That page isn't edited from Ops.")
        if await page_doc(key):
            raise HTTPException(status_code=409, detail="This page already has a draft. Reload to see it.")
    draft = R.clean(kind, key or "", data)
    if kind in ("post", "job"):
        draft["slug"] = R.slugify(draft.get("slug") or draft.get("title") or "") or f"{kind}-{uuid4().hex[:6]}"
        base = draft["slug"]
        n = 2
        while not await _slug_free(kind, draft["slug"], None):
            draft["slug"] = f"{base}-{n}"[:80]
            n += 1
        key = draft["slug"]
    now = _now()
    doc = {
        "id": uuid4().hex, "kind": kind, "key": key, "draft": draft, "draft_version": 1, "live": None, "live_version": 0,
        "live_at": None, "schedule_at": None, "review": None, "sent_back": None, "created_at": now, "updated_at": now,
        "created_by": by_of(staff), "updated_by": by_of(staff), "deleted": False,
    }
    await content_col().insert_one(dict(doc))
    return doc


async def save_draft(content_id: str, data: dict[str, Any], base_version: int, staff: dict[str, Any]) -> dict[str, Any]:
    """Saves what staff typed. A save made from an older copy is refused, so two people never overwrite each other. Changing a
    draft that was waiting for review or set for later takes it back to a draft: it has to be approved again."""
    doc = await get(content_id)
    draft = R.clean(doc["kind"], doc["key"], data)
    sets: dict[str, Any] = {"draft": draft, "updated_at": _now(), "updated_by": by_of(staff)}
    if doc["kind"] in ("post", "job"):
        slug = R.slugify(draft.get("slug") or "")
        if not slug:
            raise HTTPException(status_code=400, detail="Add a web address.")
        if not await _slug_free(doc["kind"], slug, content_id):
            raise HTTPException(status_code=409, detail="Another post already uses that web address. Choose another.")
        draft["slug"] = slug
        sets["key"] = slug
    if draft == doc.get("draft"):
        return doc
    if doc.get("review") or doc.get("schedule_at"):
        sets.update({"review": None, "schedule_at": None, "scheduled_by": None})
    res = await content_col().find_one_and_update(
        {"id": content_id, "draft_version": int(base_version), "deleted": {"$ne": True}},
        {"$set": sets, "$inc": {"draft_version": 1}}, projection={"_id": 0}, return_document=True,
    )
    if not res:
        raise HTTPException(status_code=409, detail="Someone else saved this a moment ago. Reload to see their change, then make yours.")
    return res


async def send_for_review(content_id: str, note: str, staff: dict[str, Any]) -> dict[str, Any]:
    doc = await get(content_id)
    if status_of(doc) != "draft":
        raise HTTPException(status_code=400, detail="Only a draft with changes can be sent for review.")
    found = R.problems(doc["kind"], doc["key"], doc["draft"])
    if found:
        raise HTTPException(status_code=400, detail=found[0]["message"])
    review = {"by": staff.get("id"), "by_name": by_of(staff)["name"], "at": _now(), "note": note.strip()}
    await content_col().update_one({"id": content_id}, {"$set": {"review": review, "sent_back": None}})
    return await get(content_id)


async def send_back(content_id: str, note: str, staff: dict[str, Any]) -> dict[str, Any]:
    doc = await get(content_id)
    if not doc.get("review"):
        raise HTTPException(status_code=400, detail="This isn't waiting for review.")
    sent = {"by": staff.get("id"), "by_name": by_of(staff)["name"], "at": _now(), "note": note.strip()}
    await content_col().update_one({"id": content_id}, {"$set": {"review": None, "sent_back": sent}})
    return await get(content_id)


async def _go_live(doc: dict[str, Any], who: dict[str, Any], note: str) -> None:
    """Copies the draft that was checked to live. A draft changed in the meantime is not put live: the change has to be approved."""
    version = int(doc.get("live_version") or 0) + 1
    now = _now()
    res = await content_col().update_one(
        {"id": doc["id"], "draft_version": doc["draft_version"], "live_version": doc.get("live_version") or 0},
        {"$set": {"live": doc["draft"], "live_version": version, "live_at": now, "live_by": who, "review": None, "sent_back": None,
                  "schedule_at": None, "scheduled_by": None}},
    )
    if not res.modified_count:
        raise HTTPException(status_code=409, detail="This changed a moment ago. Reload, check the change, then put it live.")
    await versions_col().insert_one({
        "id": uuid4().hex, "content_id": doc["id"], "version": version, "data": doc["draft"], "at": now, "by": who, "note": note,
    })


async def publish(content_id: str, at: Optional[datetime], note: str, staff: dict[str, Any]) -> dict[str, Any]:
    """Puts the draft live now, or sets it to go live at a time. It must pass every check first."""
    doc = await get(content_id)
    if doc.get("live") is not None and doc.get("live") == doc.get("draft") and not at:
        raise HTTPException(status_code=400, detail="This is already live. Change something first.")
    found = R.problems(doc["kind"], doc["key"], doc["draft"])
    if found:
        raise HTTPException(status_code=400, detail=found[0]["message"])
    if at and at > _now():
        await content_col().update_one({"id": content_id}, {"$set": {"schedule_at": at, "scheduled_by": by_of(staff),
                                                                     "schedule_note": note.strip(), "review": None}})
    else:
        await _go_live(doc, by_of(staff), note.strip())
    return await get(content_id)


async def cancel_schedule(content_id: str) -> dict[str, Any]:
    await get(content_id)
    await content_col().update_one({"id": content_id}, {"$set": {"schedule_at": None, "scheduled_by": None}})
    return await get(content_id)


async def unpublish(content_id: str) -> dict[str, Any]:
    doc = await get(content_id)
    if doc["kind"] not in ("post", "job"):
        raise HTTPException(status_code=400, detail="A page can't be taken down. Put an earlier version back instead.")
    await content_col().update_one({"id": content_id}, {"$set": {"live": None, "live_at": None, "schedule_at": None}})
    return await get(content_id)


async def remove(content_id: str) -> None:
    doc = await get(content_id)
    if doc["kind"] not in ("post", "job"):
        raise HTTPException(status_code=400, detail="A page can't be deleted.")
    if doc.get("live") is not None:
        raise HTTPException(status_code=400, detail="Take it off the website first, then delete it.")
    await content_col().update_one({"id": content_id}, {"$set": {"deleted": True, "deleted_at": _now()}})


async def versions(content_id: str) -> list[dict[str, Any]]:
    await get(content_id)
    rows = await versions_col().find({"content_id": content_id}, {"_id": 0, "data": 0}).sort("version", -1).to_list(200)
    return [{**r, "by": (r.get("by") or {}).get("name")} for r in rows]


async def restore(content_id: str, version: int, staff: dict[str, Any]) -> dict[str, Any]:
    """Copies an earlier live version into the draft. It goes live only when approved again."""
    doc = await get(content_id)
    old = await versions_col().find_one({"content_id": content_id, "version": int(version)}, {"_id": 0})
    if not old:
        raise HTTPException(status_code=404, detail="That version doesn't exist.")
    return await save_draft(content_id, old["data"], doc["draft_version"], staff)


_last_promote = 0.0


async def promote_due(force: bool = False) -> int:
    """Puts live everything whose set time has come. Called by the website and Ops reads, at most every 20 seconds per server."""
    global _last_promote
    if not force and time.monotonic() - _last_promote < 20:
        return 0
    _last_promote = time.monotonic()
    done = 0
    async for doc in content_col().find({"schedule_at": {"$ne": None, "$lte": _now()}, "deleted": {"$ne": True}}, {"_id": 0}):
        # only one server makes the change: the first to clear the time
        claimed = await content_col().find_one_and_update(
            {"id": doc["id"], "schedule_at": doc["schedule_at"]}, {"$set": {"schedule_at": None}}, projection={"_id": 0})
        if claimed and R.problems(claimed["kind"], claimed["key"], claimed["draft"]) == []:
            try:
                await _go_live(claimed, claimed.get("scheduled_by") or {"name": "Set time"}, claimed.get("schedule_note") or "")
                done += 1
            except HTTPException:
                pass  # the draft changed after it was set; it waits to be approved again
    return done


async def create_indexes() -> None:
    await content_col().create_index("id", unique=True)
    await content_col().create_index([("kind", 1), ("key", 1)], unique=True, partialFilterExpression={"deleted": False})
    await content_col().create_index([("kind", 1), ("updated_at", -1)])
    await content_col().create_index("schedule_at", sparse=True)
    await content_col().create_index([("kind", 1), ("live.slug", 1)])
    await versions_col().create_index([("content_id", 1), ("version", -1)], unique=True)
