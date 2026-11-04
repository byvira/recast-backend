"""Where a person is signed in: one small record for each sign-in, so the Security tab can list the devices and end any of them.

The tokens still decide who is allowed in (see app.core.auth); this record only describes a session so a person can recognise it.
Ending one makes none of its tokens work again, the same as signing out there."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Request

from app.core.auth import get_token_from_request, is_session_revoked, revoke_session, session_id_of
from app.db.mongo import get_db
from app.pipelines.publish.spine import iso_utc

#: How often a session's "last active" moves forward, so a busy tab doesn't write on every refresh.
TOUCH_EVERY = timedelta(minutes=10)
MAX_LISTED = 50


def _col():
    return get_db()["auth_sessions"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def device_label(agent: str) -> str:
    """"Chrome on Windows": the browser and system a person would recognise, from the browser's own description."""
    ua = agent or ""
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in ua), "")
    system = next((name for key, name in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"), ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"),
    ) if key in ua), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "A device"


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip() or (request.client.host if request.client else "")) or ""


async def record(sid: Optional[str], user_id: str, request: Request) -> None:
    """Note a new sign-in. Never raises: a failed note must not stop someone signing in."""
    if not sid:
        return
    try:
        now = _now()
        await _col().update_one(
            {"sid": sid},
            {"$set": {"user_id": user_id, "ip": client_ip(request), "device": device_label(request.headers.get("user-agent", "")), "last_seen_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass


async def touch(sid: Optional[str]) -> None:
    """A refresh means the session is in use: move its "last active" forward, at most every few minutes."""
    if not sid:
        return
    try:
        now = _now()
        await _col().update_one({"sid": sid, "last_seen_at": {"$lt": now - TOUCH_EVERY}}, {"$set": {"last_seen_at": now}})
    except Exception:  # noqa: BLE001
        pass


def current_sid(request: Request) -> Optional[str]:
    return session_id_of(get_token_from_request(request)) or session_id_of(request.cookies.get("refresh_token"))


async def listing(user_id: str, request: Request) -> list[dict[str, Any]]:
    """The person's sessions, the one they are using first. A session that was ended elsewhere is dropped from the list."""
    here = current_sid(request)
    if here and not await _col().find_one({"sid": here}, {"_id": 1}):
        # signed in before sessions were noted: the one in use is added now, so it always shows
        await record(here, user_id, request)
    rows = await _col().find({"user_id": user_id}, {"_id": 0}).sort("last_seen_at", -1).to_list(MAX_LISTED)
    out: list[dict[str, Any]] = []
    for row in rows:
        if await is_session_revoked(row["sid"]):
            await _col().delete_one({"sid": row["sid"]})
            continue
        out.append({
            "id": row["sid"], "device": row.get("device") or "A device", "ip": row.get("ip") or "",
            "created_at": iso_utc(row.get("created_at")), "last_seen_at": iso_utc(row.get("last_seen_at")), "current": row["sid"] == here,
        })
    out.sort(key=lambda r: (not r["current"], 0))
    return out


async def end_one(user_id: str, sid: str) -> bool:
    """End one of this person's sessions. False when it isn't theirs or is already gone."""
    row = await _col().find_one({"sid": sid, "user_id": user_id}, {"_id": 1})
    if not row:
        return False
    await revoke_session(sid)
    await _col().delete_one({"sid": sid})
    return True


async def forget(sid: Optional[str]) -> None:
    if sid:
        await _col().delete_one({"sid": sid})


async def forget_all(user_id: str) -> None:
    await _col().delete_many({"user_id": user_id})
