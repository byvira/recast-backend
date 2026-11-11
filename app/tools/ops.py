"""What Ops sees and controls about the tool layer: usage, every call, the switches, every key, and what looks like abuse.

All of it is read from records the gatekeeper already writes (`tool_calls`, `tool_changes`, `api_keys`, `tool_settings`). A switch changes what
the gatekeeper answers on the very next call. Every change made here is written to the staff audit with who, what, before and after."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.mongo import get_db
from app.tools import ledger
from app.tools.registry import get_registry

#: a key whose last hour looks like this is listed in the abuse watch
MIN_CALLS = 20
REFUSAL_SHARE = 0.5
AUTH_FAILURES_PER_HOUR = 20
RATE_LIMITED_PER_HOUR = 10
CREDITS_PER_HOUR = 200
FAILURE_KEEP_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None


def failures():
    return get_db()["tool_auth_failures"]


async def create_indexes() -> None:
    await failures().create_index("at", expireAfterSeconds=FAILURE_KEEP_HOURS * 3600)


def who_hash(address: str) -> str:
    """Sign-in failures are counted by a short hash of the caller's address, so the log shows a pattern and never the address."""
    return hashlib.sha256(address.encode("utf-8")).hexdigest()[:12]


async def record_auth_failure(address: str) -> None:
    await failures().insert_one({"at": _now(), "who": who_hash(address)})


# ── usage ────────────────────────────────────────────────────────────────

async def overview(hours: int = 24) -> dict[str, Any]:
    since = _now() - timedelta(hours=hours)
    base = {"at": {"$gte": since}}
    calls = ledger.calls()
    total = await calls.count_documents(base)
    by_outcome = {r["_id"]: r["n"] async for r in calls.aggregate([{"$match": base}, {"$group": {"_id": "$outcome", "n": {"$sum": 1}}}])}
    refusals = [{"code": r["_id"], "count": r["n"]} async for r in calls.aggregate([
        {"$match": {**base, "outcome": "refused"}}, {"$group": {"_id": "$code", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 15}])]
    top_tools = [{"tool": r["_id"], "calls": r["n"], "credits": r["credits"]} async for r in calls.aggregate([
        {"$match": base}, {"$group": {"_id": "$tool", "n": {"$sum": 1}, "credits": {"$sum": {"$ifNull": ["$charged_credits", 0]}}}}, {"$sort": {"n": -1}},
        {"$limit": 15}, {"$project": {"n": 1, "credits": 1}}])]
    surfaces = {r["_id"] or "unknown": r["n"] async for r in calls.aggregate([{"$match": base}, {"$group": {"_id": "$surface", "n": {"$sum": 1}}}])}
    top_workspaces = [{"workspace_id": r["_id"], "calls": r["n"], "credits": r["credits"]} async for r in calls.aggregate([
        {"$match": {**base, "workspace_id": {"$ne": None}}}, {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}, "credits": {"$sum": {"$ifNull": ["$charged_credits", 0]}}}},
        {"$sort": {"n": -1}}, {"$limit": 10}])]
    changes = await get_db()["tool_changes"].count_documents({"at": {"$gte": since}})
    undone = await get_db()["tool_changes"].count_documents({"at": {"$gte": since}, "undone_by": {"$ne": None}})
    return {
        "hours": hours, "calls": total, "by_outcome": by_outcome, "refusals": refusals, "top_tools": top_tools, "surfaces": surfaces, "top_workspaces": top_workspaces,
        "changes": changes, "undone": undone, "tools_in_catalog": len(get_registry()),
        "keys": {"active": await get_db()["api_keys"].count_documents({"tool_scopes": {"$exists": True}, "revoked": {"$ne": True}}),
                 "revoked": await get_db()["api_keys"].count_documents({"tool_scopes": {"$exists": True}, "revoked": True})},
        "switches": await switch_state(),
    }


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r.get("id"), "at": _iso(r.get("at")), "workspace_id": r.get("workspace_id"), "surface": r.get("surface"), "credential_id": r.get("credential_id"),
            "tool": r.get("tool"), "risk": r.get("risk"), "outcome": r.get("outcome"), "code": r.get("code"), "estimate_credits": r.get("estimate_credits"),
            "charged_credits": r.get("charged_credits"), "dry_run": bool(r.get("dry_run")), "fields": r.get("fields") or [], "duration_ms": r.get("duration_ms")}


async def calls_page(*, workspace_id: Optional[str], tool: Optional[str], outcome: Optional[str], code: Optional[str], credential_id: Optional[str],
                     limit: int, before: Optional[datetime]) -> dict[str, Any]:
    q: dict[str, Any] = {}
    for key, value in (("workspace_id", workspace_id), ("tool", tool), ("outcome", outcome), ("code", code), ("credential_id", credential_id)):
        if value:
            q[key] = value
    if before:
        q["at"] = {"$lt": before}
    rows = await ledger.calls().find(q, {"_id": 0}).sort("at", -1).limit(limit + 1).to_list(length=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [_row(r) for r in rows], "next_before": _iso(rows[-1]["at"]) if more and rows else None}


# ── the catalog as Ops sees it ───────────────────────────────────────────

async def tool_table(days: int = 7) -> list[dict[str, Any]]:
    since = _now() - timedelta(days=days)
    stats = {r["_id"]: r async for r in ledger.calls().aggregate([
        {"$match": {"at": {"$gte": since}}},
        {"$group": {"_id": "$tool", "calls": {"$sum": 1}, "refused": {"$sum": {"$cond": [{"$eq": ["$outcome", "refused"]}, 1, 0]}},
                    "failed": {"$sum": {"$cond": [{"$eq": ["$outcome", "failed"]}, 1, 0]}}, "credits": {"$sum": {"$ifNull": ["$charged_credits", 0]}}}}])}
    off = {r["tool"]: r for r in await ledger.settings().find({"scope": "tool", "off": True}).to_list(length=1000)}
    from app.tools import executor, undo

    out = []
    for t in get_registry().all():
        s = stats.get(t.name, {})
        out.append({"name": t.name, "summary": t.summary, "risk": t.risk.value, "scope": t.scope.value, "mode": t.mode.value, "cost": t.cost_key or t.cost_note or "free",
                    "wired": executor.is_wired(t), "undo": undo.rule_for(t.name).kind.value if undo.rule_for(t.name) else None,
                    "off": t.name in off, "off_message": (off.get(t.name) or {}).get("message"), "calls": s.get("calls", 0), "refused": s.get("refused", 0),
                    "failed": s.get("failed", 0), "credits": s.get("credits", 0)})
    return out


# ── switches ─────────────────────────────────────────────────────────────

async def switch_state() -> dict[str, Any]:
    rows = await ledger.settings().find({}, {"_id": 0}).to_list(length=2000)
    return {
        "global": next(({"off": bool(r.get("off")), "message": r.get("message")} for r in rows if r.get("scope") == "global"), {"off": False, "message": None}),
        "tools_off": sorted(r["tool"] for r in rows if r.get("scope") == "tool" and r.get("off")),
        "workspaces": [{"workspace_id": r["workspace_id"], "all_off": bool(r.get("all_off")), "off_tools": r.get("off_tools") or []}
                       for r in rows if r.get("scope") == "workspace" and (r.get("all_off") or r.get("off_tools"))],
    }


async def set_global(off: bool, message: Optional[str]) -> dict[str, Any]:
    before = (await switch_state())["global"]
    await ledger.settings().update_one({"scope": "global"}, {"$set": {"scope": "global", "off": off, "message": (message or "").strip()[:200] or None, "at": _now()}}, upsert=True)
    return {"before": before, "after": (await switch_state())["global"]}


async def set_tool(tool: str, off: bool, message: Optional[str]) -> dict[str, Any]:
    if get_registry().get(tool) is None:
        raise KeyError(tool)
    before = tool in (await switch_state())["tools_off"]
    await ledger.settings().update_one({"scope": "tool", "tool": tool}, {"$set": {"scope": "tool", "tool": tool, "off": off, "message": (message or "").strip()[:200] or None, "at": _now()}}, upsert=True)
    return {"before": before, "after": off}


async def set_workspace(workspace_id: str, all_off: bool, off_tools: list[str]) -> dict[str, Any]:
    names = {t.name for t in get_registry().all()}
    unknown = [t for t in off_tools if t not in names]
    if unknown:
        raise KeyError(unknown[0])
    before = next((s for s in (await switch_state())["workspaces"] if s["workspace_id"] == workspace_id), {"workspace_id": workspace_id, "all_off": False, "off_tools": []})
    await ledger.settings().update_one({"scope": "workspace", "workspace_id": workspace_id},
                                       {"$set": {"scope": "workspace", "workspace_id": workspace_id, "all_off": all_off, "off_tools": sorted(set(off_tools)), "at": _now()}}, upsert=True)
    return {"before": before, "after": {"workspace_id": workspace_id, "all_off": all_off, "off_tools": sorted(set(off_tools))}}


# ── keys ─────────────────────────────────────────────────────────────────

async def keys(*, workspace_id: Optional[str], limit: int = 100) -> list[dict[str, Any]]:
    q: dict[str, Any] = {"tool_scopes": {"$exists": True}}
    if workspace_id:
        q["workspace_id"] = workspace_id
    docs = await get_db()["api_keys"].find(q, {"hash": 0, "_id": 0}).sort("created_at", -1).limit(limit).to_list(length=limit)
    since = _now() - timedelta(days=7)
    counts = {r["_id"]: r["n"] async for r in ledger.calls().aggregate([
        {"$match": {"credential_id": {"$in": [d["id"] for d in docs]}, "at": {"$gte": since}}}, {"$group": {"_id": "$credential_id", "n": {"$sum": 1}}}])}
    return [{"id": d["id"], "workspace_id": d["workspace_id"], "name": d["name"], "prefix": d["prefix"], "tool_scopes": d.get("tool_scopes") or [],
             "capabilities": d.get("capabilities") or [], "credit_cap": d.get("credit_cap"), "created_by": d.get("created_by"), "created_at": _iso(d.get("created_at")),
             "last_used_at": _iso(d.get("last_used_at")), "expires_at": _iso(d.get("expires_at")), "revoked": bool(d.get("revoked")), "calls_7d": counts.get(d["id"], 0)} for d in docs]


async def revoke_key(key_id: str) -> Optional[dict[str, Any]]:
    doc = await get_db()["api_keys"].find_one_and_update({"id": key_id, "revoked": {"$ne": True}}, {"$set": {"revoked": True, "revoked_at": _now(), "revoked_by": "ops"}},
                                                         projection={"id": 1, "workspace_id": 1, "name": 1, "_id": 0})
    return doc


# ── abuse watch ──────────────────────────────────────────────────────────

async def abuse(hours: int = 1) -> dict[str, Any]:
    since = _now() - timedelta(hours=hours)
    base = {"at": {"$gte": since}}
    findings: list[dict[str, Any]] = []

    async for r in ledger.calls().aggregate([
        {"$match": {**base, "credential_id": {"$ne": None}}},
        {"$group": {"_id": "$credential_id", "calls": {"$sum": 1}, "refused": {"$sum": {"$cond": [{"$eq": ["$outcome", "refused"]}, 1, 0]}},
                    "limited": {"$sum": {"$cond": [{"$eq": ["$code", "rate_limited"]}, 1, 0]}}, "credits": {"$sum": {"$ifNull": ["$charged_credits", {"$ifNull": ["$estimate_credits", 0]}]}},
                    "workspace_id": {"$first": "$workspace_id"}}}]):
        if r["calls"] >= MIN_CALLS and r["refused"] / r["calls"] >= REFUSAL_SHARE:
            findings.append({"kind": "mostly_refused", "credential_id": r["_id"], "workspace_id": r["workspace_id"], "calls": r["calls"], "refused": r["refused"],
                             "why": "More than half of this key's calls are being refused: a broken script, or someone testing what it may do."})
        if r["limited"] >= RATE_LIMITED_PER_HOUR:
            findings.append({"kind": "rate_limited", "credential_id": r["_id"], "workspace_id": r["workspace_id"], "calls": r["calls"], "refused": r["limited"],
                             "why": "This key keeps hitting its requests-a-minute limit."})
        if r["credits"] >= CREDITS_PER_HOUR:
            findings.append({"kind": "credit_burst", "credential_id": r["_id"], "workspace_id": r["workspace_id"], "calls": r["calls"], "credits": r["credits"],
                             "why": "This key used a lot of credits in an hour."})

    async for r in failures().aggregate([{"$match": base}, {"$group": {"_id": "$who", "n": {"$sum": 1}}}, {"$match": {"n": {"$gte": AUTH_FAILURES_PER_HOUR}}}, {"$sort": {"n": -1}}]):
        findings.append({"kind": "guessing_keys", "who": r["_id"], "failures": r["n"], "why": "Many calls with a key that does not exist: someone may be guessing keys."})
    return {"hours": hours, "findings": findings, "thresholds": {"min_calls": MIN_CALLS, "refusal_share": REFUSAL_SHARE, "auth_failures_per_hour": AUTH_FAILURES_PER_HOUR,
                                                                "rate_limited_per_hour": RATE_LIMITED_PER_HOUR, "credits_per_hour": CREDITS_PER_HOUR}}
