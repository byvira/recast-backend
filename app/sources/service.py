"""Sources as the member and the scheduler see them: make one, check it, pause it, and run the checks that are due.

A source is a feed address, a page address, or a file (a spreadsheet, a document or a picture) that is read once and drafted from a few rows at a time. It is checked at most every `MIN_HOURS` hours, as the member who made it and
only while they are still in the workspace. It stops itself, with a plain reason in Activity, when its address fails three checks in a row, when the member leaves,
when the workspace has switched sources off, or when credits run out (it waits and carries on). It can only ever make drafts (see `flow.py`)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db
from app.pipelines.publish.generic.safe_url import UnsafeUrl, check_https_url
from app.sources import documents, feeds, flow, pictures
from app.sources.fetch import FetchError, Fetched, fetch
from app.sources.items import Item, ReadError, item_id, plain

logger = logging.getLogger(__name__)

KINDS = ("feed", "page", "file")
PENDING_KEEP_DAYS = 30
MIN_HOURS = 6
DEFAULT_HOURS = 6
MAX_SOURCES = 20
FAILS_BEFORE_PAUSE = 3
MAX_DUE_PER_TICK = 20
HREF = "/dashboard/library"


class SourceError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def col():
    return get_db()["sources"]


async def create_indexes() -> None:
    try:
        await col().create_index([("workspace_id", 1), ("created_at", -1)])
        await col().create_index([("status", 1), ("next_check_at", 1)])
        await col().create_index("id", unique=True)
        await flow.create_indexes()
        await pending_col().create_index([("source_id", 1), ("seq", 1)])
        await pending_col().create_index("created_at", expireAfterSeconds=PENDING_KEEP_DAYS * 24 * 3600)
    except Exception as exc:  # noqa: BLE001 - optional
        logger.warning("Could not create the source indexes: %s", exc)


def public(doc: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "brand_id", "name", "kind", "address", "platforms", "mode", "relevance", "daily_limit", "status", "check_every_hours", "paused_reason", "file_kind", "file_rows")
    out = {k: doc.get(k) for k in keys}
    health = doc.get("health") or {}
    out["health"] = {"fail_streak": int(health.get("fail_streak") or 0), "last_error": health.get("last_error"), "last_ok_at": _iso(health.get("last_ok_at"))}
    out.update({"last_run_at": _iso(doc.get("last_run_at")), "next_check_at": _iso(doc.get("next_check_at")), "created_at": _iso(doc.get("created_at")), "counts": doc.get("counts") or {}})
    return out


def _iso(value: Any) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None


def _address(raw: str) -> str:
    address = (raw or "").strip()
    try:
        check_https_url(address)
    except UnsafeUrl as exc:
        raise SourceError("address_refused", str(exc), 422) from exc
    return address


async def _common_checks(workspace_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """What every new source must satisfy, whatever it reads: a known way of working, one of this workspace's brands, at least one platform, and room for another source."""
    if body.get("mode", "link") not in flow.MODES or body.get("relevance", "medium") not in flow.THRESHOLDS:
        raise SourceError("input_invalid", "Choose link or write, and everything, medium or close.", 422)
    brand = await get_db()["brand_profiles"].find_one({"id": body.get("brand_id"), "workspace_id": workspace_id}, {"_id": 0, "id": 1})
    if brand is None:
        raise SourceError("brand_unknown", "Choose one of this workspace's brands.", 422)
    platforms = [p for p in dict.fromkeys(body.get("platforms") or []) if isinstance(p, str) and 0 < len(p) <= 30][:6]
    if not platforms:
        raise SourceError("input_invalid", "Choose at least one platform.", 422)
    if await col().count_documents({"workspace_id": workspace_id, "status": {"$ne": "removed"}}) >= MAX_SOURCES:
        raise SourceError("sources_full", f"A workspace can follow {MAX_SOURCES} sources. Remove one first.", 409)
    return brand, platforms


async def create(workspace_id: str, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("kind", "feed")
    if kind not in KINDS:
        raise SourceError("input_invalid", "A source is a feed or a page.", 422)
    if kind == "file":
        raise SourceError("input_invalid", "Add a file with the file option.", 422)
    address = _address(body.get("address", ""))
    brand, platforms = await _common_checks(workspace_id, body)
    if await col().count_documents({"workspace_id": workspace_id, "address": address, "status": {"$ne": "removed"}}):
        raise SourceError("duplicate_source", "You already follow that address.", 409)
    hours = max(MIN_HOURS, min(int(body.get("check_every_hours") or DEFAULT_HOURS), 168))
    now = _now()
    doc = {
        "id": uuid4().hex, "workspace_id": workspace_id, "brand_id": brand["id"], "created_by": user_id, "name": (body.get("name") or address).strip()[:80], "kind": kind,
        "address": address, "platforms": platforms, "mode": body.get("mode", "link"), "relevance": body.get("relevance", "medium"), "include_older": bool(body.get("include_older")),
        "daily_limit": max(1, min(int(body.get("daily_limit") or flow.DEFAULT_DAILY_PER_SOURCE), 30)), "status": "active", "paused_reason": None,
        "check_every_hours": hours, "next_check_at": now, "last_run_at": None, "created_at": now, "health": {"fail_streak": 0}, "counts": {"drafted": 0, "skipped": 0, "failed": 0},
    }
    await col().insert_one(dict(doc))
    return public(doc)


async def _load(workspace_id: str, source_id: str) -> dict[str, Any]:
    doc = await col().find_one({"id": source_id, "workspace_id": workspace_id, "status": {"$ne": "removed"}}, {"_id": 0})
    if doc is None:
        raise SourceError("not_found", "That source was not found.", 404)
    return doc


async def list_sources(workspace_id: str) -> list[dict[str, Any]]:
    rows = await col().find({"workspace_id": workspace_id, "status": {"$ne": "removed"}}, {"_id": 0}).sort("created_at", -1).to_list(length=MAX_SOURCES + 5)
    return [public(r) for r in rows]


async def get(workspace_id: str, source_id: str) -> dict[str, Any]:
    return public(await _load(workspace_id, source_id))


async def items_of(workspace_id: str, source_id: str, limit: int = 50) -> list[dict[str, Any]]:
    await _load(workspace_id, source_id)
    rows = await flow.items_col().find({"workspace_id": workspace_id, "source_id": source_id}, {"_id": 0}).sort("seen_at", -1).limit(max(1, min(limit, 100))).to_list(length=100)
    return [{**r, "seen_at": _iso(r.get("seen_at"))} for r in rows]


VERDICTS = ("good", "not_for_me", "none")


async def rate_item(workspace_id: str, source_id: str, item_id: str, verdict: str) -> dict[str, Any]:
    """A person's tap on one item: a good one, or not for them. It is only recorded (it feeds the results and the suggestions); it never changes a setting by itself."""
    if verdict not in VERDICTS:
        raise SourceError("input_invalid", "Choose good, not for me, or none.", 422)
    await _load(workspace_id, source_id)
    change = {"$set": {"feedback": verdict, "feedback_at": _now()}} if verdict != "none" else {"$unset": {"feedback": "", "feedback_at": ""}}
    done = await flow.items_col().update_one({"workspace_id": workspace_id, "source_id": source_id, "item_id": item_id}, change)
    if done.matched_count == 0:
        raise SourceError("not_found", "That item was not found.", 404)
    return {"item_id": item_id, "feedback": None if verdict == "none" else verdict}


async def update(workspace_id: str, source_id: str, body: dict[str, Any]) -> dict[str, Any]:
    await _load(workspace_id, source_id)
    changes: dict[str, Any] = {}
    if "name" in body:
        changes["name"] = str(body["name"]).strip()[:80] or "Source"
    if "mode" in body:
        if body["mode"] not in flow.MODES:
            raise SourceError("input_invalid", "Choose link or write.", 422)
        changes["mode"] = body["mode"]
    if "relevance" in body:
        if body["relevance"] not in flow.THRESHOLDS:
            raise SourceError("input_invalid", "Choose everything, medium or close.", 422)
        changes["relevance"] = body["relevance"]
    if "daily_limit" in body:
        changes["daily_limit"] = max(1, min(int(body["daily_limit"]), 30))
    if "check_every_hours" in body:
        changes["check_every_hours"] = max(MIN_HOURS, min(int(body["check_every_hours"]), 168))
    if "platforms" in body:
        platforms = [p for p in dict.fromkeys(body["platforms"] or []) if isinstance(p, str) and 0 < len(p) <= 30][:6]
        if not platforms:
            raise SourceError("input_invalid", "Choose at least one platform.", 422)
        changes["platforms"] = platforms
    if changes:
        await col().update_one({"id": source_id, "workspace_id": workspace_id}, {"$set": changes})
    return await get(workspace_id, source_id)


async def pause(workspace_id: str, source_id: str, reason: str = "You paused it.") -> dict[str, Any]:
    await _load(workspace_id, source_id)
    await col().update_one({"id": source_id, "workspace_id": workspace_id}, {"$set": {"status": "paused", "paused_reason": reason}})
    return await get(workspace_id, source_id)


async def resume(workspace_id: str, source_id: str) -> dict[str, Any]:
    await _load(workspace_id, source_id)
    await col().update_one({"id": source_id, "workspace_id": workspace_id}, {"$set": {"status": "active", "paused_reason": None, "next_check_at": _now(), "health.fail_streak": 0}})
    return await get(workspace_id, source_id)


async def remove(workspace_id: str, source_id: str) -> None:
    await _load(workspace_id, source_id)
    await col().update_one({"id": source_id, "workspace_id": workspace_id}, {"$set": {"status": "removed", "removed_at": _now()}})
    await pending_col().delete_many({"source_id": source_id, "workspace_id": workspace_id})


# ── files ────────────────────────────────────────────────────────────────────

def pending_col():
    return get_db()["source_pending"]


async def pictures_today(workspace_id: str) -> int:
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    return await col().count_documents({"workspace_id": workspace_id, "file_kind": documents.IMAGE, "created_at": {"$gte": start}})


async def preview_file(workspace_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """What a file would give, without making a source or spending anything: its kind, how many items, the columns of a table, and the first few items.
    A picture is not read here (reading it uses the shared picture allowance), so it only says how many pictures are left today."""
    kind = documents.sniff(data, filename)
    if kind == documents.IMAGE:
        if not pictures.mime_of(data):
            raise SourceError("unsupported", "Pictures can be JPG, PNG or WebP.", 422)
        left = pictures.allowance_left(await pictures_today(workspace_id))
        return {"kind": kind, "title": re.sub(r"\.[A-Za-z0-9]+$", "", filename or "Picture")[:80] or "Picture", "count": 1, "columns": [], "first": [], "pictures_left_today": left}
    try:
        title, items = documents.read(filename, data)
        columns = documents.columns_of(filename, data)
    except ReadError as exc:
        raise SourceError(exc.code, exc.message, 422) from exc
    if not items:
        raise SourceError("empty", "Nothing could be read in that file.", 422)
    return {"kind": kind, "title": title, "count": len(items), "columns": columns, "first": [{"title": i.title, "summary": i.text[:200]} for i in items[:3]]}


async def create_file(workspace_id: str, user_id: str, filename: str, data: bytes, body: dict[str, Any]) -> dict[str, Any]:
    """A source from a file: a spreadsheet (a row is an item), a document (one item) or a picture (one item). It is read once, now; the items wait and are drafted a few at a
    time by the normal checks, under the same limits as any source. Nothing is posted."""
    import hashlib

    brand, platforms = await _common_checks(workspace_id, body)
    kind = documents.sniff(data, filename)
    digest = hashlib.sha256(data).hexdigest()[:16]
    address = f"file:{digest}"
    if await col().count_documents({"workspace_id": workspace_id, "address": address, "status": {"$ne": "removed"}}):
        raise SourceError("duplicate_source", "You already added that file.", 409)
    try:
        if kind == documents.IMAGE:
            if pictures.allowance_left(await pictures_today(workspace_id)) <= 0:
                raise SourceError("pictures_full", f"A workspace can add {pictures.DAILY_PICTURES} pictures a day. Try again tomorrow.", 429)
            title, items = await pictures.read_picture(data, filename, workspace_id)
        else:
            title, items = documents.read(filename, data, title_column=body.get("title_column") or None, text_columns=body.get("text_columns") or None)
    except ReadError as exc:
        raise SourceError(exc.code, exc.message, 422) from exc
    if not items:
        raise SourceError("empty", "Nothing could be read in that file.", 422)
    now = _now()
    source_id = uuid4().hex
    rows = []
    for number, item in enumerate(items):
        item.id = item_id("", f"{digest}:{item.id}", item.title)           # the same row in two different files is not a repeat
        rows.append({"workspace_id": workspace_id, "source_id": source_id, "seq": number, "created_at": now,
                     "item": {"id": item.id, "title": item.title, "text": item.text, "link": item.link, "kind": item.kind, "extra": item.extra}})
    doc = {
        "id": source_id, "workspace_id": workspace_id, "brand_id": brand["id"], "created_by": user_id, "name": (body.get("name") or title).strip()[:80], "kind": "file", "address": address,
        "file_kind": kind, "file_rows": len(items), "platforms": platforms, "mode": body.get("mode", "link"), "relevance": body.get("relevance", "medium"), "include_older": True,
        "daily_limit": max(1, min(int(body.get("daily_limit") or flow.DEFAULT_DAILY_PER_SOURCE), 30)), "status": "active", "paused_reason": None, "check_every_hours": DEFAULT_HOURS,
        "next_check_at": now, "last_run_at": None, "created_at": now, "health": {"fail_streak": 0}, "counts": {"drafted": 0, "skipped": 0, "failed": 0},
    }
    await col().insert_one(dict(doc))
    await pending_col().insert_many(rows, ordered=False)
    return public(doc)


async def read_pending(source: dict[str, Any]) -> list[Item]:
    """The next items of a file source that have not been handled yet, in file order."""
    docs = await pending_col().find({"source_id": source["id"], "workspace_id": source["workspace_id"]}).sort("seq", 1).limit(flow.MAX_PER_PASS * 4).to_list(length=flow.MAX_PER_PASS * 4)
    return [Item(**d["item"]) for d in docs]


async def _settle_file(source: dict[str, Any], items: list[Item]) -> None:
    """Drop the items that were handled (drafted, skipped or failed all leave a record) and, when none are left, finish the source."""
    ids = [i.id for i in items]
    handled = {d["item_id"] async for d in flow.items_col().find({"workspace_id": source["workspace_id"], "item_id": {"$in": ids}}, {"item_id": 1})}
    if handled:
        await pending_col().delete_many({"source_id": source["id"], "workspace_id": source["workspace_id"], "item.id": {"$in": list(handled)}})
    if not await pending_col().count_documents({"source_id": source["id"], "workspace_id": source["workspace_id"]}, limit=1):
        await col().update_one({"id": source["id"]}, {"$set": {"status": "paused", "paused_reason": "Finished: everything in this file has been read."}})


# ── reading ──────────────────────────────────────────────────────────────────

async def read_address(kind: str, got: Fetched) -> list[Item]:
    """The items at a fetched address. A feed gives its items; a page gives itself as one item (a changed page is a new item); a PDF link gives the document."""
    body, ctype = got.body, got.content_type
    if feeds.looks_like_feed(body, ctype):
        return feeds.parse(body)[1]
    if body[:4] == b"%PDF" or ctype == "application/pdf":
        return documents.read(got.url.rsplit("/", 1)[-1] or "document.pdf", body)[1]
    if kind == "feed":
        raise ReadError("not_a_feed", "That address is not a feed Ekavo can read. A feed is an RSS or Atom address.")
    if not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype or not ctype):
        raise ReadError("not_a_page", "That address is not a web page Ekavo can read.")
    try:
        import trafilatura

        text = trafilatura.extract(body.decode("utf-8", errors="replace"), include_comments=False, include_tables=False) or ""
        title_match = re.search(r"<title[^>]*>(.*?)</title>", body[:20000].decode("utf-8", errors="replace"), re.IGNORECASE | re.DOTALL)
    except Exception as exc:  # noqa: BLE001
        raise ReadError("cant_read", "That page could not be read.") from exc
    text = plain(text, 6000)
    if len(text) < 80:
        raise ReadError("no_text", "There is hardly any text on that page to use.")
    import hashlib

    title = plain(title_match.group(1), 200) if title_match else got.url
    # A page is the same item until its words change, so the id comes from the address and the text together (a feed item is told apart by its link alone).
    version = hashlib.sha256(f"{got.url}\n{text}".encode("utf-8")).hexdigest()[:24]
    return [Item(id=version, title=title, text=text, link=got.url, kind="page")]


async def check(workspace_id: str, address: str, kind: str = "feed") -> dict[str, Any]:
    """Fetch an address once, without making anything, to show what a source would see: its first items. Used by the add flow before a source is switched on."""
    address = _address(address)
    try:
        items = await read_address(kind, await fetch(address))
    except (FetchError, ReadError) as exc:
        raise SourceError(exc.code, exc.message, 422) from exc
    return {"address": address, "count": len(items), "items": [{"title": i.title, "link": i.link, "summary": i.text[:200], "published": _iso(i.published)} for i in items[:5]]}


# ── running ──────────────────────────────────────────────────────────────────

async def _note(source: dict[str, Any], key: str, title: str, description: str, status: str = "warning") -> None:
    try:
        from app.shared.activity import record_system

        await record_system(workspace_id=source["workspace_id"], key=f"source:{source['id']}:{key}:{_now().strftime('%Y-%m-%d')}", actor_name="Sources", category="content_generated", title=title,
                            description=description, status=status, target_id=source["id"], target_type="Source", target_label=source.get("name"), href=HREF)
    except Exception as exc:  # noqa: BLE001 - a note that cannot be saved never stops a check
        logger.warning("Could not record a source note: %s", exc)


async def _fail(source: dict[str, Any], code: str, message: str, retryable: bool) -> dict[str, Any]:
    streak = int((source.get("health") or {}).get("fail_streak") or 0) + 1
    changes: dict[str, Any] = {"health.fail_streak": streak, "health.last_error": message, "health.last_error_code": code, "last_run_at": _now(),
                               "next_check_at": _now() + timedelta(hours=int(source.get("check_every_hours") or DEFAULT_HOURS))}
    if streak >= FAILS_BEFORE_PAUSE or not retryable:
        changes.update(status="paused", paused_reason=message)
        await _note(source, "paused", f"{source.get('name')} was paused", f"{message} Open Sources to fix the address or resume it.")
    await col().update_one({"id": source["id"]}, {"$set": changes, "$inc": {"counts.failed": 1}})
    return {"ok": False, "code": code, "message": message, "paused": "status" in changes}


async def run(source: dict[str, Any], *, force: bool = False, tools=None, score=None, fetcher=None) -> dict[str, Any]:
    """One check of one source. Never raises: it records what happened and returns it."""
    from app.sources import runner

    workspace_id, user_id = source["workspace_id"], source["created_by"]
    run_tool = tools or runner.tool_runner(workspace_id, user_id)
    try:
        if source.get("kind") == "file":
            items = await read_pending(source)
        else:
            got = await (fetcher or fetch)(source["address"])
            items = await read_address(source.get("kind", "feed"), got)
    except FetchError as exc:
        return await _fail(source, exc.code, exc.message, exc.retryable)
    except ReadError as exc:
        return await _fail(source, exc.code, exc.message, False)
    brand = await get_db()["brand_profiles"].find_one({"id": source["brand_id"], "workspace_id": workspace_id}, {"_id": 0}) or {}
    first = not source.get("last_run_at")
    result = await flow.process(source, items, run_tool=run_tool, brand_text=runner.brand_text_of(brand), score=score or runner.relevance_score, first_look=first)
    skipped = sum(result.skipped.values())
    if source.get("kind") == "file":
        await _settle_file(source, items)
    await col().update_one({"id": source["id"]}, {
        "$set": {"last_run_at": _now(), "next_check_at": _now() + timedelta(hours=int(source.get("check_every_hours") or DEFAULT_HOURS)), "health.fail_streak": 0, "health.last_ok_at": _now(), "health.last_error": None},
        "$inc": {"counts.drafted": result.drafted, "counts.skipped": skipped, "counts.failed": result.failed}})
    try:
        from app.sources import results

        await results.advise({**source, "advised": (await col().find_one({"id": source["id"]}, {"advised": 1}) or {}).get("advised")})
    except Exception as exc:  # noqa: BLE001 - advice never stops a check
        logger.warning("Could not write source advice: %s", type(exc).__name__)
    if result.drafted:
        await _note(source, "drafted", f"{source.get('name')}: {result.drafted} new {'draft' if result.drafted == 1 else 'drafts'}", "Drafts are waiting for your approval. Nothing was posted.", "success")
    return {"ok": True, "drafted": result.drafted, "skipped": result.skipped, "failed": result.failed, "notes": result.notes, "items_seen": len(items)}


async def run_due() -> int:
    """The scheduled pass: every active source that is due, each on its own so one failure never holds the rest."""
    from app.billing import credits
    from app.tools.services import LiveServices

    services = LiveServices()
    now = _now()
    due = await col().find({"status": "active", "next_check_at": {"$lte": now}}).to_list(length=MAX_DUE_PER_TICK)
    ran = 0
    for source in due:
        workspace_id = source["workspace_id"]
        try:
            if await services.member_permissions(workspace_id, source["created_by"]) is None:
                await col().update_one({"id": source["id"]}, {"$set": {"status": "paused", "paused_reason": "The person who set this up is no longer in the workspace."}})
                continue
            if await services.switched_off(workspace_id, "sources.run"):
                continue                                            # held where it is; it runs once the switch is released
            if source.get("mode") == "write" and not await credits.has_room(workspace_id, 1):
                await credits.pause_notice(workspace_id, source.get("name") or "A source", href=HREF, key=f"source:{source['id']}")
                continue
            await run(source)
            ran += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("A source check failed: %s", type(exc).__name__)
    return ran
