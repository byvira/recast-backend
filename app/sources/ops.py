"""What Ops sees and controls about Sources: one switch, usage, the sources that stopped themselves, and what looks like abuse.

The switch is the tool layer's own per-tool switch, set on every `sources.*` tool at once, so the API, assistants, the terminal, Ask and the scheduled checks
all stop on the next call and start again when it is released. Everything else is read from records that already exist (`sources`, `source_items`, `tool_calls`)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.mongo import get_db
from app.sources import flow
from app.tools import ledger, ops as tool_ops
from app.tools.registry import get_registry

#: a workspace that adds this many sources in a day, or has this many refused address checks in an hour, is listed in the abuse watch
NEW_SOURCES_PER_DAY = 10
REFUSED_PER_HOUR = 10
PAUSED_SOURCES = 5
LIST_LIMIT = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None


def tool_names() -> list[str]:
    return [n for n in get_registry().names() if n.startswith("sources.")]


async def switch() -> dict[str, Any]:
    state = await tool_ops.switch_state()
    off = [n for n in tool_names() if n in state["tools_off"]]
    return {"off": len(off) == len(tool_names()), "some_off": sorted(set(off)) if 0 < len(off) < len(tool_names()) else [], "tools": tool_names()}


async def set_switch(off: bool, message: Optional[str]) -> dict[str, Any]:
    before = (await switch())["off"]
    for name in tool_names():
        await tool_ops.set_tool(name, off, message)
    return {"before": before, "after": off}


async def usage(days: int = 7) -> dict[str, Any]:
    since = _now() - timedelta(days=days)
    sources = get_db()["sources"]
    by_status = {r["_id"] or "unknown": r["n"] async for r in sources.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
    outcomes = {r["_id"] or "unknown": r["n"] async for r in flow.items_col().aggregate([{"$match": {"seen_at": {"$gte": since}}}, {"$group": {"_id": "$outcome", "n": {"$sum": 1}}}])}
    top = [{"workspace_id": r["_id"], "drafted": r["n"]} async for r in flow.items_col().aggregate([
        {"$match": {"seen_at": {"$gte": since}, "outcome": "drafted"}}, {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 10}])]
    calls = [{"tool": r["_id"], "calls": r["n"], "credits": r.get("credits") or 0} async for r in ledger.calls().aggregate([
        {"$match": {"at": {"$gte": since}, "tool": {"$regex": "^sources\\."}}}, {"$group": {"_id": "$tool", "n": {"$sum": 1}, "credits": {"$sum": {"$ifNull": ["$credits", 0]}}}}, {"$sort": {"n": -1}}])]
    return {"days": days, "sources_by_status": by_status, "items_by_outcome": outcomes, "top_workspaces": top, "tool_calls": calls}


async def failures() -> list[dict[str, Any]]:
    """Sources that stopped themselves or are failing, newest trouble first."""
    q = {"$or": [{"status": "paused", "paused_reason": {"$nin": [None, ""]}}, {"health.fail_streak": {"$gt": 0}}]}
    docs = await get_db()["sources"].find(q, {"_id": 0}).sort("last_run_at", -1).limit(LIST_LIMIT).to_list(length=LIST_LIMIT)
    return [{"id": d["id"], "workspace_id": d["workspace_id"], "name": d.get("name"), "address": d.get("address"), "status": d.get("status"),
             "reason": d.get("paused_reason"), "fail_streak": int((d.get("health") or {}).get("fail_streak") or 0),
             "last_error": (d.get("health") or {}).get("last_error"), "last_run_at": _iso(d.get("last_run_at"))} for d in docs]


async def abuse() -> list[dict[str, Any]]:
    """Workspaces to look at. A flag for staff to read, never an automatic ban."""
    found: dict[str, dict[str, Any]] = {}

    def add(workspace_id: str, why: str, count: int) -> None:
        found.setdefault(workspace_id, {"workspace_id": workspace_id, "reasons": []})["reasons"].append({"why": why, "count": count})

    day = _now() - timedelta(days=1)
    hour = _now() - timedelta(hours=1)
    async for r in get_db()["sources"].aggregate([{"$match": {"created_at": {"$gte": day}}}, {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}}, {"$match": {"n": {"$gte": NEW_SOURCES_PER_DAY}}}]):
        add(r["_id"], "Added many sources in a day", r["n"])
    async for r in ledger.calls().aggregate([{"$match": {"at": {"$gte": hour}, "tool": {"$regex": "^sources\\."}, "outcome": "refused"}},
                                              {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}}, {"$match": {"n": {"$gte": REFUSED_PER_HOUR}}}]):
        add(r["_id"], "Many refused source calls in an hour", r["n"])
    async for r in get_db()["sources"].aggregate([{"$match": {"status": "paused", "paused_reason": {"$nin": [None, ""]}}},
                                                  {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}}, {"$match": {"n": {"$gte": PAUSED_SOURCES}}}]):
        add(r["_id"], "Many sources stopped themselves", r["n"])
    return sorted(found.values(), key=lambda x: -sum(r["count"] for r in x["reasons"]))[:LIST_LIMIT]


async def overview(days: int = 7) -> dict[str, Any]:
    return {"switch": await switch(), "usage": await usage(days), "failures": await failures(), "abuse": await abuse()}
