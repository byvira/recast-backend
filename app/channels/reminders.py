"""Reminders: Ekavo starts a short message in a chat app ("3 drafts are waiting for your yes") instead of only replying. Built for a free-first MVP.

* **Off by default, for everyone.** A person turns it on by sending REMIND ON from the chat (REMIND OFF turns it off). Only then is the chat's own id kept, encrypted,
  because a message that Ekavo starts needs somewhere to go (everywhere else only a hash is kept). Turning it off deletes it.
* **At most one a day for each person**, never in quiet hours (21:00 to 08:00 in the person's own time, India by default), and it is a pointer and a link, never a draft's text.
* **Telegram** has no 24-hour window and costs nothing: a reminder is an ordinary message. **WhatsApp** only allows a free-form message inside 24 hours of the person's last
  message; after that it needs an approved template, which costs money (about 12 to 15 paise in India, before GST). So WhatsApp reminders are capped at ZERO until the owner
  raises the daily and monthly caps in Ops, and need `WHATSAPP_TEMPLATE_DRAFTS` (the template's name, approved by Meta). Inside the window a reminder is free.
* **Ops sees the spend**: template messages this month and the estimated rupees, and the caps stop sends the moment they are reached. A failed send is logged and never retried.

The trigger today is the daily approvals digest (`app/tools/digest.py`): a person who opted into that and into chat reminders gets one pointer a day. Nothing sends by itself otherwise."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.channels import link as links
from app.core.config import settings
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

QUIET_START, QUIET_END = 21, 8
DEFAULT_TZ_MIN = 330                      # India
WINDOW = timedelta(hours=24)
DEFAULTS: dict[str, float] = {"whatsapp_per_day": 0, "telegram_per_day": 3, "whatsapp_per_month": 0, "template_rupees": 0.15}
ON_WORDS, OFF_WORDS = ("remind on", "reminders on"), ("remind off", "reminders off")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log():
    return get_db()["channel_reminders"]


def config():
    return get_db()["channel_settings"]


async def create_indexes() -> None:
    await log().create_index([("link_id", 1), ("at", -1)])
    await log().create_index([("workspace_id", 1), ("channel", 1), ("at", -1)])
    await log().create_index("at", expireAfterSeconds=120 * 24 * 3600)


async def limits() -> dict[str, float]:
    """The caps and the estimated cost of a template, as Ops set them (zero or the defaults until then)."""
    doc = await config().find_one({"_id": "reminders"}) or {}
    return {k: float(doc.get(k, v)) for k, v in DEFAULTS.items()}


async def set_limits(changes: dict[str, float]) -> dict[str, float]:
    clean = {k: float(v) for k, v in changes.items() if k in DEFAULTS and float(v) >= 0}
    if clean:
        await config().update_one({"_id": "reminders"}, {"$set": clean}, upsert=True)
    return await limits()


# ── opting in and out ────────────────────────────────────────────────────────

def _fernet_ready() -> bool:
    return bool(settings.FERNET_SECRET_KEY)


def parse_word(text: str) -> Optional[bool]:
    """True for REMIND ON, False for REMIND OFF, None for anything else."""
    low = (text or "").strip().lower()
    return True if low in ON_WORDS else False if low in OFF_WORDS else None


async def opt_in(link: dict[str, Any], sender: str) -> bool:
    """Keep the chat's id, encrypted, and switch reminders on. False when encryption is not set up (then nothing is kept)."""
    if not _fernet_ready():
        return False
    from app.pipelines.publish.token_store import encrypt_token

    await links.links().update_one({"id": link["id"]}, {"$set": {"reminders": True, "sender_enc": encrypt_token(sender), "tz_offset_min": link.get("tz_offset_min", DEFAULT_TZ_MIN)}})
    return True


async def opt_out(link_id: str) -> None:
    await links.links().update_one({"id": link_id}, {"$set": {"reminders": False}, "$unset": {"sender_enc": ""}})


# ── when it may be sent ──────────────────────────────────────────────────────

def is_quiet(now: datetime, tz_offset_min: int = DEFAULT_TZ_MIN) -> bool:
    hour = (now + timedelta(minutes=tz_offset_min)).hour
    return hour >= QUIET_START or hour < QUIET_END


def window_open(link: dict[str, Any], now: datetime) -> bool:
    last = link.get("last_inbound_at")
    if not isinstance(last, datetime):
        return False
    last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    return now - last < WINDOW


async def spend(month_start: Optional[datetime] = None) -> dict[str, Any]:
    """Template messages sent this month and what they cost, estimated. Free messages (inside the window, or Telegram) are counted separately."""
    now = _now()
    start = month_start or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cfg = await limits()
    templates = await log().count_documents({"channel": "whatsapp", "kind": "template", "at": {"$gte": start}})
    free = await log().count_documents({"kind": "free", "at": {"$gte": start}})
    return {"since": start.isoformat().replace("+00:00", "Z"), "templates": templates, "free": free, "estimated_rupees": round(templates * cfg["template_rupees"], 2)}


# ── sending ──────────────────────────────────────────────────────────────────

def pointer(count: int) -> str:
    where = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard"
    return f"{count} {'draft is' if count == 1 else 'drafts are'} waiting for your yes. Open: {where}"


async def nudge(workspace_id: str, user_id: str, count: int, adapters: Optional[dict[str, Any]] = None, now: Optional[datetime] = None) -> list[dict[str, str]]:
    """One reminder for each of this person's opted-in chats, if it is allowed. Returns what happened for each (for tests and logs). Never raises."""
    from app.channels import telegram, whatsapp
    from app.pipelines.publish.token_store import decrypt_token

    now = now or _now()
    adapters = adapters or {"whatsapp": whatsapp.WhatsAppAdapter(), "telegram": telegram.TelegramAdapter()}
    cfg = await limits()
    out: list[dict[str, str]] = []
    rows = await links.links().find({"workspace_id": workspace_id, "user_id": user_id, "status": "active", "reminders": True, "sender_enc": {"$exists": True}}).to_list(length=10)
    for row in rows:
        channel = row["channel"]
        outcome = {"link": row["id"], "channel": channel, "result": ""}
        out.append(outcome)
        try:
            if channel not in adapters:
                outcome["result"] = "unsupported"
            elif is_quiet(now, int(row.get("tz_offset_min", DEFAULT_TZ_MIN))):
                outcome["result"] = "quiet_hours"
            elif await log().count_documents({"link_id": row["id"], "at": {"$gte": now - timedelta(hours=20)}}):
                outcome["result"] = "already_today"
            elif await log().count_documents({"workspace_id": workspace_id, "channel": channel, "at": {"$gte": now - timedelta(hours=24)}}) >= cfg.get(f"{channel}_per_day", 0):
                outcome["result"] = "daily_cap"
            else:
                to = decrypt_token(row["sender_enc"])
                kind = "free"
                if channel == "whatsapp" and not window_open(row, now):
                    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    if not settings.WHATSAPP_TEMPLATE_DRAFTS:
                        outcome["result"] = "no_template"
                    elif await log().count_documents({"channel": "whatsapp", "kind": "template", "at": {"$gte": month_start}}) >= cfg["whatsapp_per_month"]:
                        outcome["result"] = "monthly_cap"
                    else:
                        kind = "template"
                if not outcome["result"]:
                    sent = (await adapters[channel].send_template(to, settings.WHATSAPP_TEMPLATE_DRAFTS, [str(count)]) if kind == "template"
                            else await adapters[channel].send(to, pointer(count)))
                    outcome["result"] = kind if sent else "send_failed"
                    # a failed send still counts, so a broken connection is never hammered
                    await log().insert_one({"link_id": row["id"], "workspace_id": workspace_id, "channel": channel, "kind": kind, "ok": bool(sent), "at": now})
        except Exception as exc:  # noqa: BLE001 - a reminder never stops the digest
            logger.info("A reminder could not be sent: %s", type(exc).__name__)
            outcome["result"] = "error"
    return out
