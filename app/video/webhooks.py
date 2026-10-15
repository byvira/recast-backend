"""Signed webhooks for the video pipeline. A workspace registers an https address and the events it wants; each event is queued as a
delivery, sent with a signature the receiver can check, and retried with a growing wait when it fails.

Safety: the address must be https and resolve only to public internet addresses, checked when it is saved and again before every send;
redirects are never followed; the reply is never read past a small size. The signing secret is shown once, when the webhook is made.
A webhook that keeps failing is switched off and says so."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import httpx

from app.db.mongo import get_db
from app.pipelines.publish.generic.safe_url import UnsafeUrl, assert_safe_url

logger = logging.getLogger(__name__)

EVENTS = ("video.render_done", "video.qc_failed", "video.review_answered")
MAX_HOOKS = 5
BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=12))
MAX_ATTEMPTS = len(BACKOFF) + 1
SWITCH_OFF_AFTER = 25          # failed deliveries in a row
TIMEOUT_S = 10.0
_tasks: set[asyncio.Task] = set()


class WebhookError(ValueError):
    """A refusal with a plain message."""


def _hooks():
    return get_db()["video_webhooks"]


def _deliveries():
    return get_db()["video_webhook_deliveries"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    if isinstance(v, datetime):
        v = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return v.isoformat().replace("+00:00", "Z")
    return None


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """What the receiver recomputes: sha256 of `<timestamp>.<body>` keyed by the secret."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()


def public(h: dict) -> dict[str, Any]:
    return {"id": h["id"], "url": h["url"], "events": h["events"], "active": bool(h.get("active", True)), "created_at": _iso(h.get("created_at")),
            "failures_in_a_row": int(h.get("failures", 0)), "switched_off_reason": h.get("off_reason")}


async def create(workspace_id: str, user_id: str, url: str, events: list[str]) -> tuple[dict[str, Any], str]:
    """Register a webhook. Returns (its public record, the signing secret, shown once)."""
    chosen = [e for e in dict.fromkeys(events or []) if e in EVENTS]
    if not chosen:
        raise WebhookError("Choose at least one event to be told about.")
    try:
        await assert_safe_url(url)
    except UnsafeUrl as exc:
        raise WebhookError(str(exc))
    if await _hooks().count_documents({"workspace_id": workspace_id}) >= MAX_HOOKS:
        raise WebhookError(f"A workspace can have {MAX_HOOKS} webhooks. Remove one first.")
    secret = "whsec_" + secrets.token_urlsafe(30)
    doc = {"id": uuid4().hex, "workspace_id": workspace_id, "url": url.strip(), "events": chosen, "secret": secret, "active": True, "failures": 0,
           "created_by": user_id, "created_at": _now()}
    await _hooks().insert_one(dict(doc))
    return public(doc), secret


async def list_hooks(workspace_id: str) -> list[dict[str, Any]]:
    return [public(h) for h in await _hooks().find({"workspace_id": workspace_id}).sort("created_at", -1).to_list(length=20)]


async def remove(workspace_id: str, hook_id: str) -> bool:
    res = await _hooks().delete_one({"id": hook_id, "workspace_id": workspace_id})
    return bool(res.deleted_count)


async def recent_deliveries(workspace_id: str, hook_id: str) -> list[dict[str, Any]]:
    rows = await _deliveries().find({"workspace_id": workspace_id, "hook_id": hook_id}).sort("created_at", -1).to_list(length=20)
    return [{"id": r["id"], "event": r["event"], "status": r["status"], "attempts": r["attempts"], "last_status_code": r.get("last_status_code"),
             "last_error": r.get("last_error"), "created_at": _iso(r.get("created_at")), "next_attempt_at": _iso(r.get("next_at"))} for r in rows]


def _body(delivery: dict) -> bytes:
    return json.dumps({"id": delivery["id"], "event": delivery["event"], "created_at": _iso(delivery["created_at"]), "data": delivery["data"]},
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def _attempt(delivery: dict, hook: dict) -> bool:
    """Send one delivery. True when the receiver answered 2xx. Records the outcome and schedules the next try, or gives up."""
    body = _body(delivery)
    stamp = str(int(_now().timestamp()))
    status_code: Optional[int] = None
    error: Optional[str] = None
    try:
        await assert_safe_url(hook["url"])
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client:
            resp = await client.post(hook["url"], content=body, headers={
                "Content-Type": "application/json", "User-Agent": "Ekavo-Webhooks/1", "X-Ekavo-Event": delivery["event"],
                "X-Ekavo-Delivery": delivery["id"], "X-Ekavo-Timestamp": stamp, "X-Ekavo-Signature": sign(hook["secret"], stamp, body),
            })
        status_code = resp.status_code
        ok = 200 <= resp.status_code < 300
        if not ok:
            error = f"The receiver answered {resp.status_code}."
    except UnsafeUrl as exc:
        ok, error = False, str(exc)
    except httpx.HTTPError as exc:
        ok, error = False, f"Could not reach the receiver ({type(exc).__name__})."
    attempts = int(delivery.get("attempts", 0)) + 1
    if ok:
        await _deliveries().update_one({"id": delivery["id"]}, {"$set": {"status": "delivered", "attempts": attempts, "last_status_code": status_code,
                                                                          "last_error": None, "next_at": None, "delivered_at": _now()}})
        await _hooks().update_one({"id": hook["id"]}, {"$set": {"failures": 0}})
        return True
    give_up = attempts >= MAX_ATTEMPTS
    await _deliveries().update_one({"id": delivery["id"]}, {"$set": {
        "status": "failed" if give_up else "retrying", "attempts": attempts, "last_status_code": status_code, "last_error": error,
        "next_at": None if give_up else _now() + BACKOFF[attempts - 1]}})
    failures = int(hook.get("failures", 0)) + 1
    update: dict[str, Any] = {"failures": failures}
    if failures >= SWITCH_OFF_AFTER:
        update.update({"active": False, "off_reason": "Switched off after many failed deliveries. Fix the address and add the webhook again."})
    await _hooks().update_one({"id": hook["id"]}, {"$set": update})
    return False


async def dispatch(workspace_id: str, event: str, data: dict[str, Any]) -> int:
    """Queue `event` for every active webhook of the workspace that asked for it, and start sending. Never raises. Returns how many."""
    if event not in EVENTS:
        return 0
    try:
        hooks = await _hooks().find({"workspace_id": workspace_id, "active": {"$ne": False}, "events": event}).to_list(length=MAX_HOOKS)
        for h in hooks:
            d = {"id": uuid4().hex, "workspace_id": workspace_id, "hook_id": h["id"], "event": event, "data": data, "status": "pending", "attempts": 0,
                 "created_at": _now(), "next_at": _now(), "last_status_code": None, "last_error": None}
            await _deliveries().insert_one(dict(d))
            task = asyncio.create_task(_attempt(d, h))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
        return len(hooks)
    except Exception as exc:  # noqa: BLE001 - a webhook must never fail the work that caused it
        logger.warning("Webhook dispatch for %s failed: %s", event, exc)
        return 0


async def send_test(workspace_id: str, hook_id: str) -> dict[str, Any]:
    """Send one ping to a webhook so the member can see it works. Returns the delivery's outcome."""
    h = await _hooks().find_one({"id": hook_id, "workspace_id": workspace_id})
    if not h:
        raise WebhookError("That webhook was not found.")
    d = {"id": uuid4().hex, "workspace_id": workspace_id, "hook_id": hook_id, "event": "video.render_done", "data": {"test": True}, "status": "pending",
         "attempts": MAX_ATTEMPTS - 1, "created_at": _now(), "next_at": None, "last_status_code": None, "last_error": None}
    await _deliveries().insert_one(dict(d))
    ok = await _attempt(d, h)
    fresh = await _deliveries().find_one({"id": d["id"]})
    return {"delivered": ok, "status_code": fresh.get("last_status_code"), "error": fresh.get("last_error")}


async def retry_due() -> int:
    """Send the deliveries whose wait is over. Called every minute by the scheduler."""
    due = await _deliveries().find({"status": "retrying", "next_at": {"$lte": _now()}}).to_list(length=50)
    sent = 0
    for d in due:
        h = await _hooks().find_one({"id": d["hook_id"]})
        if not h or h.get("active") is False:
            await _deliveries().update_one({"id": d["id"]}, {"$set": {"status": "failed", "last_error": "The webhook was removed or switched off.", "next_at": None}})
            continue
        claimed = await _deliveries().update_one({"id": d["id"], "status": "retrying"}, {"$set": {"status": "sending"}})
        if claimed.modified_count:
            await _attempt({**d, "status": "sending"}, h)
            sent += 1
    return sent
