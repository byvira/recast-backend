"""What Ops sees and controls about chat apps: a switch for each, how many chats are linked and active, and what looks like abuse.

The switch is the tool layer's own per-tool setting under the name `channel.<name>` (the engine asks for it on every message), so a change acts on the next
message. Usage is read from records that already exist (`channel_links`, `channel_seen`, `channel_attempts`). A flag is for staff to read, never an automatic ban."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.channels import engine, link as links, reminders
from app.core.config import settings
from app.db.mongo import get_db
from app.tools import ledger

CHANNELS = ("whatsapp", "telegram")
FLOOD_PER_HOUR = engine.PER_HOUR
WRONG_CODES = links.MAX_WRONG_CODES_PER_HOUR


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured() -> dict[str, bool]:
    """Which channels have their keys set in this deployment (a channel with no keys answers nothing)."""
    from app.channels import telegram, whatsapp

    return {"whatsapp": whatsapp.enabled(), "telegram": telegram.enabled()}


async def switches() -> dict[str, dict[str, Any]]:
    rows = await ledger.settings().find({"scope": "tool", "tool": {"$in": [f"channel.{c}" for c in CHANNELS]}}, {"_id": 0}).to_list(length=10)
    by = {r["tool"]: r for r in rows}
    return {c: {"off": bool((by.get(f"channel.{c}") or {}).get("off")), "message": (by.get(f"channel.{c}") or {}).get("message")} for c in CHANNELS}


async def set_switch(channel: str, off: bool, message: Optional[str]) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise KeyError(channel)
    before = (await switches())[channel]["off"]
    await ledger.settings().update_one(
        {"scope": "tool", "tool": f"channel.{channel}"},
        {"$set": {"scope": "tool", "tool": f"channel.{channel}", "off": off, "message": (message or "").strip()[:200] or None, "at": _now()}}, upsert=True)
    return {"before": before, "after": off}


async def usage(hours: int = 24) -> dict[str, dict[str, Any]]:
    since = _now() - timedelta(hours=hours)
    out: dict[str, dict[str, Any]] = {}
    for c in CHANNELS:
        linked = await links.links().count_documents({"channel": c, "status": "active"})
        messages = await engine.seen().count_documents({"channel": c, "at": {"$gte": since}})
        chats = len(await engine.seen().distinct("sender_hash", {"channel": c, "at": {"$gte": since}}))
        conversations = await get_db()["ask_threads"].count_documents({"surface": c, "updated_at": {"$gte": since}})
        out[c] = {"linked": linked, "messages": messages, "active_chats": chats, "conversations": conversations}
    return out


async def abuse() -> list[dict[str, Any]]:
    """Chats sending too much, and chats guessing link codes. Shown by channel and the last four characters only; the number is never stored."""
    since = _now() - timedelta(hours=1)
    found: list[dict[str, Any]] = []
    async for row in engine.seen().aggregate([
        {"$match": {"at": {"$gte": since}}}, {"$group": {"_id": {"c": "$channel", "k": "$sender_hash"}, "n": {"$sum": 1}}}, {"$match": {"n": {"$gt": FLOOD_PER_HOUR}}}, {"$sort": {"n": -1}}, {"$limit": 20},
    ]):
        link = await links.links().find_one({"channel": row["_id"]["c"], "sender_hash": row["_id"]["k"]}, {"sender_tail": 1, "workspace_id": 1})
        found.append({"channel": row["_id"]["c"], "why": "Sent more messages in an hour than allowed", "count": row["n"], "chat": (link or {}).get("sender_tail"), "workspace_id": (link or {}).get("workspace_id")})
    async for row in links.attempts().aggregate([{"$group": {"_id": "$key", "n": {"$sum": 1}}}, {"$match": {"n": {"$gte": WRONG_CODES}}}, {"$limit": 20}]):
        found.append({"channel": None, "why": "Tried many wrong link codes in an hour", "count": row["n"], "chat": None, "workspace_id": None})
    return found


async def overview(hours: int = 24) -> dict[str, Any]:
    sw, ready, use = await switches(), configured(), await usage(hours)
    return {"reminders": {"limits": await reminders.limits(), "spend": await reminders.spend()}, "hours": hours, "channels": [{"channel": c, "configured": ready[c], **sw[c], **use[c]} for c in CHANNELS], "abuse": await abuse(), "simulator": bool(settings.CHANNELS_SIMULATOR)}
