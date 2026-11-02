"""Device notifications (web push): the Inbox's notices on a member's phone or computer, like a native app, even when Ekavo is closed.

Each device a member turns notifications on for is kept (`push_devices`, one record per device, never shared across members). When a
notice lands in the Activity Log, the members it is for are worked out with the Inbox's own rules (what they may see, and whether it
is worth interrupting them for: a decision waiting, something that failed, work done for them, their own run finished), then each
gets it on every device they turned on, for the kinds they chose. A device the browser says is gone is removed; a member gets at most
`HOURLY_CAP` notifications an hour, the rest stay in the Inbox.

The words are the notice's own title and description, cleaned for a lock screen: plain, short, and never a long dash. Each kind has
its own icon, a notice about a post shows its picture where the device can, and Ekavo's mark sits in the status bar.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import settings
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

# The kinds a member can turn on or off, in the order Settings lists them.
GROUPS: dict[str, str] = {
    "needs_you": "Needs you: approvals and decisions waiting",
    "problems": "Something went wrong: a post or payment that failed",
    "ready": "Your work is ready: campaigns, pictures, audio and video",
    "updates": "Updates from Ekavo: posts published, ideas, credits and billing",
}
DEFAULT_PREFS = {key: True for key in GROUPS}
HOURLY_CAP = 20
MAX_DEVICES = 10
TITLE_MAX = 60
BODY_MAX = 140
# How long a notice may wait for a device that is off before it is no longer worth showing.
TTL_SECONDS = 24 * 3600

ICONS = {
    "failed": "failed", "needs_you": "review", "ready": "ready", "post_published": "published", "credits": "credits",
    "billing": "billing", "recommendation": "idea", "account_connected": "account", "security_event": "account",
    "workspace_alert": "account",
}


def devices_col():
    return get_db()["push_devices"]


def _rate_col():
    return get_db()["push_rate"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32]


# ── words fit for a lock screen ──────────────────────────────────────────

_DASHES = re.compile(r"\s*[‒–—―]\s*")
_SPACES = re.compile(r"\s+")


def clean(text: Optional[str], limit: int) -> str:
    """Plain words for a notification: long dashes become commas, spacing is tidied, and anything too long ends at a word with "…"."""
    value = _DASHES.sub(", ", str(text or "")).replace(" ", " ")
    value = _SPACES.sub(" ", value).strip(" ,")
    if len(value) <= limit:
        return value
    cut = value[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return f"{cut}…"


# ── who gets a notice, and as which kind ─────────────────────────────────

def group_of(entry: dict[str, Any], user_id: str) -> Optional[str]:
    """Which kind of notification this notice is for this member, or None when it is not worth interrupting them for. Mirrors the
    Inbox's rule (app.shared.activity.inbox.inbox_query)."""
    actor = entry.get("actor") or {}
    if entry.get("lane") == "active":
        return "needs_you"
    if entry.get("status") == "failed":
        return "problems"
    if entry.get("category") == "content_generated" and actor.get("user_id") == user_id:
        return "ready"
    if actor.get("user_id") == user_id:
        return None  # their own clicks are never news to them
    if actor.get("type") in ("ai_agent", "system_cron", "webhook"):
        return "ready" if entry.get("category") in ("content_generated", "voice_calibrated") else "updates"
    return None


def icon_for(entry: dict[str, Any], group: str) -> str:
    if entry.get("status") == "failed":
        name = "failed"
    elif group in ("needs_you", "ready"):
        name = ICONS[group]
    else:
        name = ICONS.get(str(entry.get("category") or ""), "update")
    return f"/icons/notify/{name}.png"


async def _picture_for(entry: dict[str, Any]) -> Optional[str]:
    """The post's first picture, shown large under the words where the device can (Android, computers)."""
    target = entry.get("target_id")
    if not target or entry.get("target_type") not in ("Draft Post", "Live Post", "Scheduled Post"):
        return None
    piece = await get_db()["content_pieces"].find_one({"piece_id": target}, {"media": 1})
    for item in (piece or {}).get("media") or []:
        kind = getattr(item.get("kind"), "value", item.get("kind"))
        url = item.get("url") or item.get("secure_url")
        if url and str(kind or "image") == "image" and str(url).startswith("https://"):
            return str(url)
    return None


async def _recipients(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """Active members who may see this notice, with the kind it is for each of them."""
    from app.shared.activity.store import is_visible_to

    out = []
    async for m in get_db()["workspace_members"].find({"workspace_id": entry.get("workspace_id"), "status": "active"}, {"user_id": 1, "role": 1}):
        uid = m.get("user_id")
        if not uid or uid in (entry.get("hidden_by") or []) or not is_visible_to(entry, uid, str(m.get("role") or "")):
            continue
        group = group_of(entry, uid)
        if group:
            out.append((uid, group))
    return out


async def _prefs(user_id: str) -> dict[str, bool]:
    doc = await get_db()["users"].find_one({"id": user_id}, {"push_prefs": 1}) or {}
    return {**DEFAULT_PREFS, **{k: bool(v) for k, v in (doc.get("push_prefs") or {}).items() if k in GROUPS}}


async def _under_cap(user_id: str) -> bool:
    hour = _now().strftime("%Y%m%d%H")
    doc = await _rate_col().find_one_and_update(
        {"_id": f"{user_id}:{hour}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": _now()}}, upsert=True, return_document=True,
    )
    return int((doc or {}).get("n") or 0) <= HOURLY_CAP


async def _unread(workspace_id: str, user_id: str, role: str) -> Optional[int]:
    from app.shared.activity import inbox

    try:
        return await inbox.unread_count(workspace_id, user_id, role)
    except Exception:  # noqa: BLE001 - the count on the app icon is a nicety
        return None


# ── sending ──────────────────────────────────────────────────────────────

def _send_one(device: dict[str, Any], payload: dict[str, Any], urgency: str) -> Optional[int]:
    """One notification to one device. Returns the push service's error status, or None when it was accepted."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={"endpoint": device["endpoint"], "keys": device["keys"]},
            data=json.dumps(payload, separators=(",", ":")),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.VAPID_SUBJECT},
            ttl=TTL_SECONDS,
            headers={"Urgency": urgency, "Topic": str(payload.get("tag") or "")[:32]},
        )
        return None
    except WebPushException as exc:
        return getattr(exc.response, "status_code", None) or 500


async def send_to_user(user_id: str, payload: dict[str, Any], *, urgency: str = "normal") -> int:
    """Send to every device the member turned on. Gone devices are removed. Returns how many devices accepted it."""
    if not enabled():
        return 0
    sent = 0
    async for device in devices_col().find({"user_id": user_id}):
        status = await asyncio.to_thread(_send_one, device, payload, urgency)
        if status is None:
            sent += 1
            await devices_col().update_one({"_id": device["_id"]}, {"$set": {"last_ok_at": _now(), "fails": 0}})
        elif status in (404, 410):
            await devices_col().delete_one({"_id": device["_id"]})  # the browser dropped it: unsubscribed or reinstalled
        else:
            fails = int(device.get("fails") or 0) + 1
            if fails >= 5:
                await devices_col().delete_one({"_id": device["_id"]})
            else:
                await devices_col().update_one({"_id": device["_id"]}, {"$set": {"fails": fails, "last_error": status}})
    return sent


# Work still going on, or planned for later, is told once it has an outcome.
NOT_YET = ("running", "pending", "planned", "scheduled", "queued", "in_progress", "processing")


async def notify_entry(entry: dict[str, Any]) -> None:
    """A notice landed in the Activity Log: tell the members it is for, on their devices. Never raises."""
    try:
        if not enabled() or not entry.get("workspace_id"):
            return
        # a decision someone already made, or work with no outcome yet, is not news for a lock screen
        if entry.get("decided_at") or str(entry.get("status") or "") in NOT_YET:
            return
        title = clean(entry.get("title"), TITLE_MAX)
        if not title:
            return
        body = clean(entry.get("description") or entry.get("target_label"), BODY_MAX)
        ws = await get_db()["workspaces"].find_one({"id": entry["workspace_id"]}, {"name": 1}) or {}
        picture = await _picture_for(entry)
        roles = {m["user_id"]: str(m.get("role") or "") async for m in get_db()["workspace_members"].find(
            {"workspace_id": entry["workspace_id"], "status": "active"}, {"user_id": 1, "role": 1})}
        for user_id, group in await _recipients(entry):
            if not (await _prefs(user_id)).get(group, True):
                continue
            if not await devices_col().find_one({"user_id": user_id}, {"_id": 1}):
                continue
            if not await _under_cap(user_id):
                continue
            payload = {
                "title": title, "body": body, "icon": icon_for(entry, group), "badge": "/icons/notify/badge.png",
                "tag": str(entry.get("_id") or ""), "url": entry.get("href") or "/dashboard/activity",
                "workspace_id": entry["workspace_id"], "workspace": clean(ws.get("name"), 40), "group": group,
                "unread": await _unread(entry["workspace_id"], user_id, roles.get(user_id, "")),
                "at": int(_now().timestamp() * 1000),
            }
            if picture:
                payload["image"] = picture
            await send_to_user(user_id, payload, urgency="high" if group in ("needs_you", "problems") else "normal")
    except Exception:  # noqa: BLE001 - a notification never breaks the work it reports
        logger.exception("Device notification for %s failed", entry.get("_id"))


_tasks: set[asyncio.Task] = set()


def notify_soon(entry: dict[str, Any]) -> None:
    """Send in the background, so saving the notice never waits on push services."""
    try:
        task = asyncio.get_running_loop().create_task(notify_entry(dict(entry)))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ── devices ──────────────────────────────────────────────────────────────

async def save_device(user_id: str, subscription: dict[str, Any], label: str) -> dict[str, Any]:
    endpoint = str(subscription.get("endpoint") or "")
    keys = subscription.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("This device didn't give a usable address for notifications.")
    did = device_id(endpoint)
    # a device moves to whoever signs in on it now: one person's notices never reach another's phone
    await devices_col().delete_many({"_id": did, "user_id": {"$ne": user_id}})
    await devices_col().update_one(
        {"_id": did},
        {"$set": {"user_id": user_id, "endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                  "label": label[:60] or "This device", "updated_at": _now(), "fails": 0},
         "$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )
    # the oldest go first past the limit
    extra = await devices_col().find({"user_id": user_id}, {"_id": 1}).sort("updated_at", -1).skip(MAX_DEVICES).to_list(None)
    if extra:
        await devices_col().delete_many({"_id": {"$in": [d["_id"] for d in extra]}})
    return {"id": did}


async def forget_device(user_id: str, *, endpoint: Optional[str] = None, did: Optional[str] = None) -> int:
    key = did or (device_id(endpoint) if endpoint else None)
    if not key:
        return 0
    res = await devices_col().delete_one({"_id": key, "user_id": user_id})
    return res.deleted_count


async def list_devices(user_id: str) -> list[dict[str, Any]]:
    rows = await devices_col().find({"user_id": user_id}, {"endpoint": 0, "keys": 0}).sort("updated_at", -1).to_list(MAX_DEVICES)
    return [{"id": r["_id"], "label": r.get("label") or "A device", "added_at": r.get("created_at"), "last_ok_at": r.get("last_ok_at")} for r in rows]


async def get_prefs(user_id: str) -> dict[str, bool]:
    return await _prefs(user_id)


async def set_prefs(user_id: str, changes: dict[str, Any]) -> dict[str, bool]:
    clean_changes = {f"push_prefs.{k}": bool(v) for k, v in changes.items() if k in GROUPS}
    if clean_changes:
        await get_db()["users"].update_one({"id": user_id}, {"$set": clean_changes})
    return await _prefs(user_id)


async def create_indexes() -> None:
    await devices_col().create_index([("user_id", 1), ("updated_at", -1)])
    await _rate_col().create_index("at", expireAfterSeconds=2 * 3600)
