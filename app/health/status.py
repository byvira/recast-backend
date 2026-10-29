"""The public status: each part of Ekavo a member uses, whether it is working right now, its last 90 days, and incidents.

Every minute `collect` works out each part's state from what Ekavo already knows: the AI provider health (a part works while
any of its backups works), the render machine's check-ins, recent publishing failures per platform across workspaces, and how
recent emails and file uploads went. The state is kept per part with the time it began, a minute is added to the part's day,
and an incident opens by itself when a part has been down for five minutes (staff then write the updates). The collector
also writes a heartbeat, so after a crash or a long restart the gap is recorded as an outage when the server comes back.

States: operational, degraded, down, maintenance. No provider names ever reach the public answer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from app.db.mongo import get_db

logger = logging.getLogger(__name__)

STATES = ("operational", "degraded", "down", "maintenance")
STATE_TEXT = {"operational": "Working", "degraded": "Slow or partly working", "down": "Not working", "maintenance": "Maintenance"}
RANK = {"operational": 0, "maintenance": 1, "degraded": 2, "down": 3}
DAYS = 90
AUTO_OPEN_AFTER = timedelta(minutes=5)
#: a part only slow or partly working opens a notice by itself after half an hour, so a passing slow spell is not news
AUTO_OPEN_DEGRADED_AFTER = timedelta(minutes=30)
AUTO_RESOLVE_AFTER = timedelta(minutes=10)
#: a gap in the heartbeat longer than this is an outage; after a clean shutdown (a deploy) a longer gap is allowed
GAP_AFTER_CRASH = timedelta(minutes=3)
GAP_AFTER_DEPLOY = timedelta(minutes=10)

PUBLISH_PLATFORMS: dict[str, str] = {
    "linkedin": "LinkedIn", "twitter": "X", "instagram": "Instagram", "facebook": "Facebook", "youtube": "YouTube",
    "threads": "Threads", "bluesky": "Bluesky",
}

# Each part: who it is for, and where its state comes from. `providers` are AI providers that back each other up.
COMPONENTS: list[dict[str, Any]] = [
    {"id": "app", "label": "Sign-in and saving your work", "group": "Ekavo", "source": "server"},
    {"id": "uploads", "label": "Uploads and files", "group": "Ekavo", "source": "signal", "signal": "storage"},
    {"id": "writing", "label": "Writing posts", "group": "Making content", "source": "providers",
     "providers": ["groq", "gemini", "mistral", "openrouter", "nvidia"]},
    {"id": "pictures", "label": "Pictures", "group": "Making content", "source": "providers",
     "providers": ["cloudflare", "gemini", "huggingface", "pollinations"]},
    {"id": "voice", "label": "Voice and audio", "group": "Making content", "source": "providers", "providers": ["elevenlabs", "deepgram"]},
    {"id": "video", "label": "Making videos", "group": "Making content", "source": "render"},
    *[{"id": f"publish_{key}", "label": label, "group": "Publishing", "source": "publish", "platform": key}
      for key, label in PUBLISH_PLATFORMS.items()],
    {"id": "email", "label": "Emails from Ekavo", "group": "Ekavo", "source": "signal", "signal": "email"},
]
BY_ID = {c["id"]: c for c in COMPONENTS}


def _db():
    return get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


# ── working out each part's state ────────────────────────────────────────

def from_counts(attempts: int, successes: int) -> Optional[str]:
    """A state from recent attempts, or None when there were too few to say."""
    if attempts < 3:
        return None
    rate = successes / attempts
    if rate < 0.5:
        return "down"
    if rate < 0.9:
        return "degraded"
    return "operational"


def from_providers(states: dict[str, str]) -> str:
    """A part backed by several providers works while any of them works. Providers with nothing recent say nothing."""
    seen = [s for s in states.values() if s != "idle"]
    if not seen or "healthy" in seen:
        return "operational"
    if any(s in ("degraded", "limit_reached") for s in seen):
        return "degraded"
    return "down"


async def _provider_states() -> dict[str, str]:
    try:
        from app.shared.llm_health import health
        from app.shared.llm_health.service import provider_states

        return {k: v["status"] for k, v in (await provider_states()).items() if health.is_configured(k)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Status: could not read the AI provider health: %s", exc)
        return {}


async def _render_state() -> Optional[str]:
    from app.core.config import settings
    from app.video import jobs

    if not settings.VIDEO_WORKER_ENABLED:
        return None
    if await jobs.worker_ready():
        return "operational"
    waiting = await _db()["video_jobs"].find_one({"status": "queued"}, sort=[("created_at", 1)])
    if not waiting:
        return "degraded"
    age = _now() - (_aware(waiting.get("created_at")) or _now())
    return "down" if age > timedelta(minutes=15) else "degraded"


async def _publish_states() -> dict[str, str]:
    """Per platform: failures that are not one member's own sign-in (FATAL), across several workspaces, with few posts getting
    through, over the last 30 minutes."""
    since = _now() - timedelta(minutes=30)
    failures: dict[str, set[str]] = {}
    async for row in _db()["publish_incidents"].find({"created_at": {"$gte": since}, "error_type": {"$ne": "AUTH"}}, {"platform": 1, "workspace_id": 1}):
        failures.setdefault(str(row.get("platform") or ""), set()).add(str(row.get("workspace_id") or ""))
    out: dict[str, str] = {}
    for key in PUBLISH_PLATFORMS:
        workspaces = len(failures.get(key, set()))
        if workspaces < 2:
            out[key] = "operational"
            continue
        went_out = await _db()["content_pieces"].count_documents({"publish_target": key, "published_at": {"$gte": since}})
        out[key] = "down" if went_out == 0 and workspaces >= 3 else "degraded"
    return out


async def current_states() -> dict[str, str]:
    from app.health import signals

    providers = await _provider_states()
    publish = await _publish_states()
    render = await _render_state()
    out: dict[str, str] = {}
    for c in COMPONENTS:
        source = c["source"]
        if source == "server":
            out[c["id"]] = "operational"                       # the collector is running, so the server and database answer
        elif source == "providers":
            out[c["id"]] = from_providers({p: providers[p] for p in c["providers"] if p in providers})
        elif source == "render":
            if render is not None:
                out[c["id"]] = render
        elif source == "publish":
            out[c["id"]] = publish.get(c["platform"], "operational")
        elif source == "signal":
            attempts, ok = signals.recent(c["signal"])
            out[c["id"]] = from_counts(attempts, ok) or "operational"
    return out


# ── maintenance windows and incidents ────────────────────────────────────

def incidents_col():
    return _db()["status_incidents"]


def _in_maintenance(incident: dict, now: datetime) -> bool:
    start, end = _aware(incident.get("scheduled_for")), _aware(incident.get("scheduled_until"))
    return bool(incident.get("kind") == "maintenance" and incident.get("status") != "completed" and start and end and start <= now < end)


async def _next_number() -> int:
    doc = await _db()["error_counters"].find_one_and_update({"_id": "incidents"}, {"$inc": {"n": 1}}, upsert=True, return_document=True)
    return int(doc["n"])


async def open_incident(
    *, title: str, components: list[str], impact: str, text: str, by: Optional[str] = None, auto: bool = False,
    kind: str = "incident", status: str = "investigating", started_at: Optional[datetime] = None,
    scheduled_for: Optional[datetime] = None, scheduled_until: Optional[datetime] = None, resolved_at: Optional[datetime] = None,
    error_numbers: Optional[list[int]] = None,
) -> dict[str, Any]:
    now = _now()
    doc = {
        "_id": uuid4().hex, "number": await _next_number(), "kind": kind, "title": title[:160], "components": components,
        "impact": impact, "status": status, "auto": auto, "started_at": started_at or now, "resolved_at": resolved_at,
        "scheduled_for": scheduled_for, "scheduled_until": scheduled_until, "created_at": now,
        "updates": [{"at": now, "status": status, "text": text[:2000], "by": by}], "error_numbers": list(error_numbers or []),
    }
    await incidents_col().insert_one(doc)
    if error_numbers:
        from app.health import errors

        await errors.issues_col().update_many({"number": {"$in": list(error_numbers)}}, {"$set": {"incident_id": doc["_id"]}})
    forget()
    _spawn(announce(doc))
    return doc


async def add_update(incident_id: str, *, status: str, text: str, by: Optional[str], impact: Optional[str] = None) -> Optional[dict]:
    now = _now()
    done = status in ("resolved", "completed")
    change: dict[str, Any] = {"$push": {"updates": {"at": now, "status": status, "text": text[:2000], "by": by}}, "$set": {"status": status}}
    if done:
        change["$set"]["resolved_at"] = now
    if impact:
        change["$set"]["impact"] = impact
    if by:
        change["$set"]["staff_touched"] = True
    doc = await incidents_col().find_one_and_update({"_id": incident_id}, change, return_document=True)
    forget()
    if doc:
        _spawn(announce(doc))
    return doc


async def set_writeup(incident_id: str, text: str, by: str) -> Optional[dict]:
    """What happened and what was changed, written after a notice is closed. Shown on the notice's public page."""
    doc = await incidents_col().find_one_and_update(
        {"_id": incident_id}, {"$set": {"writeup": text.strip()[:5000] or None, "writeup_by": by, "writeup_at": _now()}}, return_document=True,
    )
    forget()
    return doc


async def _auto_incidents(states: dict[str, str], since: dict[str, datetime], now: datetime) -> None:
    """Open a notice for a part down five minutes, or slow for thirty, with none open; raise an automatic one to "not working" when
    the part goes down; close an automatic one once every part in it has worked for ten minutes."""
    open_docs = await incidents_col().find({"kind": "incident", "status": {"$nin": ["resolved"]}}).to_list(200)
    covered = {c: d for d in open_docs for c in d.get("components", [])}
    for cid, state in states.items():
        started = since.get(cid) or now
        label = BY_ID[cid]["label"]
        existing = covered.get(cid)
        if existing:
            if state == "down" and existing.get("impact") == "degraded" and existing.get("auto") and not existing.get("staff_touched"):
                await add_update(existing["_id"], status="investigating", impact="down", by=None,
                                 text=f"{label} has stopped working for some people. We are looking into it.")
            continue
        if state == "down" and now - started >= AUTO_OPEN_AFTER:
            await open_incident(
                title=f"{label} is not working", components=[cid], impact="down", auto=True, started_at=started,
                text=f"{label} is not working for some people. We are looking into it.",
            )
        elif state == "degraded" and now - started >= AUTO_OPEN_DEGRADED_AFTER:
            await open_incident(
                title=f"{label} is slow", components=[cid], impact="degraded", auto=True, started_at=started,
                text=f"{label} is slow or partly working for some people. We are looking into it.",
            )
    for doc in open_docs:
        if not doc.get("auto") or doc.get("staff_touched"):
            continue
        parts = doc.get("components") or []
        if parts and all(states.get(c) == "operational" and now - (since.get(c) or now) >= AUTO_RESOLVE_AFTER for c in parts):
            await add_update(doc["_id"], status="resolved", text="This is working again.", by=None)


async def _maintenance_steps(now: datetime) -> list[dict]:
    """Planned maintenance moves on by itself: in progress when its window starts, done when it ends. Returns the ones not done."""
    running = []
    async for doc in incidents_col().find({"kind": "maintenance", "status": {"$ne": "completed"}}):
        start, end = _aware(doc.get("scheduled_for")), _aware(doc.get("scheduled_until"))
        if end and now >= end:
            await add_update(doc["_id"], status="completed", text="The maintenance is done and everything is back.", by=None)
            continue
        if start and now >= start and doc.get("status") == "scheduled":
            doc = await add_update(doc["_id"], status="in_progress", text="The maintenance has started.", by=None) or doc
        running.append(doc)
    return running


# ── telling people: Activity and email ──────────────────────────────────

_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _active_workspaces(limit: int = 2000) -> list[str]:
    """Workspaces with a member signed in in the last 30 days: the ones whose Activity should carry a status notice."""
    since = _now() - timedelta(days=30)
    user_ids = await _db()["users"].distinct("id", {"last_active": {"$gte": since}})
    if not user_ids:
        return []
    ids = await _db()["workspace_members"].distinct("workspace_id", {"user_id": {"$in": user_ids}, "status": "active"})
    return list(ids)[:limit]


def _site_url() -> str:
    from app.core.config import settings

    return (settings.FRONTEND_URL or "").rstrip("/")


#: Activity rows written at the same time when a notice goes out to every active workspace.
ANNOUNCE_AT_ONCE = 20


async def announce(doc: dict) -> None:
    """A notice opened or moved on: one row per workspace in Activity (the same row is updated as the notice moves on), and an
    email to everyone subscribed to status updates. Never raises."""
    try:
        from app.shared.activity.projector import record_system

        latest = (doc.get("updates") or [{}])[-1]
        done = doc.get("status") in ("resolved", "completed")
        row_status = "success" if done else ("failed" if doc.get("impact") == "down" else "warning")
        gate = asyncio.Semaphore(ANNOUNCE_AT_ONCE)

        async def one(ws_id: str) -> None:
            async with gate:
                await record_system(
                    workspace_id=ws_id, key=f"status:{doc['_id']}:{ws_id}", actor_name="Ekavo", category="status",
                    title=doc.get("title") or "Status notice", description=latest.get("text") or "", status=row_status,
                    href=f"/status/incidents/{doc['_id']}",
                )

        await asyncio.gather(*(one(ws_id) for ws_id in await _active_workspaces()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Status notice %s did not reach Activity: %s", doc.get("_id"), exc)
    try:
        await email_subscribers(doc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Status notice %s was not emailed: %s", doc.get("_id"), exc)


# ── email subscriptions ──────────────────────────────────────────────────

def subscribers_col():
    return _db()["status_subscribers"]


async def _send(to: str, subject: str, text: str) -> bool:
    from app.core.config import settings
    from app.core.notifications import sender_for

    if settings.ENVIRONMENT != "production" or not settings.RESEND_API_KEY:
        logger.info("[status email, not sent outside production] to %s: %s", to, subject)
        return True
    try:
        import resend

        resend.api_key = settings.RESEND_API_KEY
        html = "<p>" + text.replace("&", "&amp;").replace("<", "&lt;").replace(NEWLINE, "<br>") + "</p>"
        await asyncio.to_thread(resend.Emails.send, {"from": sender_for("Ekavo Status"), "to": to, "subject": subject, "html": html})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Status email to a subscriber failed: %s", exc)
        return False


NEWLINE = chr(10)


async def subscribe(email: str) -> None:
    """Ask to get status updates by email. A confirmation link is sent first, so nobody can sign someone else up. The answer is the
    same whether or not the address was already on the list."""
    import secrets

    address = email.strip().lower()
    existing = await subscribers_col().find_one({"_id": address})
    if existing and existing.get("confirmed"):
        return
    token = secrets.token_urlsafe(24)
    await subscribers_col().update_one(
        {"_id": address}, {"$set": {"token": token, "confirmed": False, "asked_at": _now()}, "$setOnInsert": {"created_at": _now()}}, upsert=True,
    )
    link = f"{_site_url()}/status/subscription?action=confirm&token={token}"
    await _send(address, "Confirm your Ekavo status updates", NEWLINE.join([
        "Confirm that you want an email when something is wrong with Ekavo, and when it is fixed:", link, "",
        "If you did not ask for this, ignore this email.",
    ]))


async def confirm(token: str) -> bool:
    res = await subscribers_col().update_one({"token": token}, {"$set": {"confirmed": True, "confirmed_at": _now()}})
    return res.matched_count > 0


async def unsubscribe(token: str) -> bool:
    res = await subscribers_col().delete_one({"token": token})
    return res.deleted_count > 0


STEP_WORDS = {
    "investigating": "Looking into it", "identified": "Cause found", "monitoring": "Fixed, watching closely", "resolved": "Resolved",
    "scheduled": "Planned", "in_progress": "In progress", "completed": "Done",
}


async def email_subscribers(doc: dict) -> int:
    """The notice's latest update to every confirmed subscriber, each with their own link to stop."""
    latest = (doc.get("updates") or [{}])[-1]
    word = STEP_WORDS.get(doc.get("status", ""), "Update")
    sent = 0
    async for sub in subscribers_col().find({"confirmed": True}).limit(5000):
        stop = f"{_site_url()}/status/subscription?action=unsubscribe&token={sub['token']}"
        text = NEWLINE.join([latest.get("text") or "", "", f"Read every update: {_site_url()}/status/incidents/{doc['_id']}", "",
                             f"Stop these emails: {stop}"])
        if await _send(sub["_id"], f"{word}: {doc.get('title') or 'Ekavo status'}", text):
            sent += 1
    return sent


# ── the minute's work ────────────────────────────────────────────────────

def _day(now: datetime) -> str:
    return now.date().isoformat()


async def collect() -> dict[str, str]:
    """Run every minute: work out each part's state, keep when it began, add the minute to its day, and open or close
    automatic incidents. Writes the heartbeat last."""
    now = _now()
    states = await current_states()
    maintenance = await _maintenance_steps(now)
    for doc in maintenance:
        if _in_maintenance(doc, now):
            for cid in doc.get("components", []):
                if cid in states:
                    states[cid] = "maintenance"
    db = _db()
    since: dict[str, datetime] = {}
    previous = {d["_id"]: d async for d in db["status_components"].find({})}
    for cid, state in states.items():
        before = previous.get(cid)
        began = _aware(before.get("since")) if before and before.get("state") == state else now
        since[cid] = began or now
        await db["status_components"].update_one({"_id": cid}, {"$set": {"state": state, "since": since[cid], "checked_at": now}}, upsert=True)
        await db["status_daily"].update_one(
            {"_id": f"{cid}:{_day(now)}"}, {"$inc": {f"minutes.{state}": 1}, "$setOnInsert": {"component": cid, "day": _day(now)}}, upsert=True,
        )
    # a part no longer checked (video making switched off on this server) is dropped, so the page never shows a stale state for it
    gone = [cid for cid in previous if cid not in states]
    if gone:
        await db["status_components"].delete_many({"_id": {"$in": gone}})
    await _auto_incidents({k: v for k, v in states.items() if v != "maintenance"}, since, now)
    await db["status_meta"].update_one({"_id": "heartbeat"}, {"$set": {"at": now}}, upsert=True)
    forget()
    return states


async def on_startup() -> Optional[dict]:
    """After a crash or a long restart, record the time nobody could reach Ekavo: the missing minutes count as down on every
    part's day, and a resolved incident says how long it was."""
    db = _db()
    meta = await db["status_meta"].find_one({"_id": "heartbeat"})
    if not meta or not meta.get("at"):
        return None
    last = _aware(meta["at"])
    now = _now()
    clean = _aware((await db["status_meta"].find_one({"_id": "shutdown"}) or {}).get("at"))
    allowed = GAP_AFTER_DEPLOY if clean and clean >= last else GAP_AFTER_CRASH
    gap = now - last
    if gap <= allowed:
        return None
    minutes = int(gap.total_seconds() // 60)
    cursor = last
    while cursor < now:
        day_end = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        chunk = int((min(day_end, now) - cursor).total_seconds() // 60)
        if chunk > 0:
            for c in COMPONENTS:
                await db["status_daily"].update_one(
                    {"_id": f"{c['id']}:{cursor.date().isoformat()}"},
                    {"$inc": {"minutes.down": chunk}, "$setOnInsert": {"component": c["id"], "day": cursor.date().isoformat()}}, upsert=True,
                )
        cursor = day_end
    await db["status_meta"].update_one({"_id": "heartbeat"}, {"$set": {"at": now}}, upsert=True)
    return await open_incident(
        title="Ekavo could not be reached", components=[c["id"] for c in COMPONENTS], impact="down", auto=True, started_at=last,
        status="resolved", resolved_at=now,
        text=f"Ekavo could not be reached for about {minutes} minutes. Everything is working again, and nothing you saved was lost.",
    )


async def on_shutdown() -> None:
    try:
        await _db()["status_meta"].update_one({"_id": "shutdown"}, {"$set": {"at": _now()}}, upsert=True)
    except Exception:  # noqa: BLE001
        pass


# ── what the public page reads ───────────────────────────────────────────

_cache: tuple[float, dict] | None = None
CACHE_SECONDS = 30


def forget() -> None:
    global _cache
    _cache = None


def day_state(minutes: dict[str, int]) -> str:
    """One day's colour: down when it was down for five minutes or more, degraded when partly, otherwise working."""
    down, degraded = int(minutes.get("down", 0)), int(minutes.get("degraded", 0))
    if down >= 5:
        return "down"
    if down > 0 or degraded >= 15:
        return "degraded"
    if not any(minutes.values()):
        return "none"
    if minutes.get("maintenance", 0) and not minutes.get("operational", 0):
        return "maintenance"
    return "operational"


def public_incident(doc: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    def iso(v: Any) -> Optional[str]:
        v = _aware(v) if isinstance(v, datetime) else None
        return v.isoformat() if v else None

    out = {
        "id": doc["_id"], "number": doc.get("number"), "kind": doc.get("kind"), "title": doc.get("title"), "impact": doc.get("impact"),
        "status": doc.get("status"), "components": [{"id": c, "label": BY_ID[c]["label"]} for c in doc.get("components", []) if c in BY_ID],
        "started_at": iso(doc.get("started_at")), "resolved_at": iso(doc.get("resolved_at")),
        "scheduled_for": iso(doc.get("scheduled_for")), "scheduled_until": iso(doc.get("scheduled_until")),
        "updates": [{"at": iso(u.get("at")), "status": u.get("status"), "text": u.get("text")} for u in reversed(doc.get("updates") or [])],
        "writeup": doc.get("writeup"),
    }
    if not full:
        out["updates"] = out["updates"][:1]
    return out


async def summary() -> dict[str, Any]:
    """Everything the public page shows, cached for half a minute."""
    global _cache
    if _cache and _cache[0] > time.monotonic():
        return _cache[1]
    db = _db()
    now = _now()
    current = {d["_id"]: d async for d in db["status_components"].find({})}
    first_day = (now - timedelta(days=DAYS - 1)).date()
    days = [first_day + timedelta(days=i) for i in range(DAYS)]
    daily: dict[str, dict[str, dict]] = {}
    async for row in db["status_daily"].find({"day": {"$gte": first_day.isoformat()}}):
        daily.setdefault(row["component"], {})[row["day"]] = row.get("minutes") or {}
    components = []
    for c in COMPONENTS:
        cur = current.get(c["id"])
        if c["source"] == "render" and not cur:
            continue                                          # video making is not switched on on this server
        state = (cur or {}).get("state") or "operational"
        rows = daily.get(c["id"], {})
        total = sum(sum(m.values()) for m in rows.values())
        down = sum(int(m.get("down", 0)) for m in rows.values())
        components.append({
            "id": c["id"], "label": c["label"], "group": c["group"], "state": state, "state_text": STATE_TEXT[state],
            "uptime": round(100 * (1 - down / total), 2) if total else None,
            "days": [{"day": d.isoformat(), "state": day_state(rows.get(d.isoformat(), {}))} for d in days],
        })
    open_docs = await incidents_col().find({"status": {"$nin": ["resolved", "completed"]}}).sort("started_at", -1).to_list(50)
    active = [public_incident(d) for d in open_docs if d.get("kind") == "incident" or _in_maintenance(d, now)]
    upcoming = [public_incident(d) for d in open_docs if d.get("kind") == "maintenance" and not _in_maintenance(d, now)
                and (_aware(d.get("scheduled_for")) or now) > now]
    recent_docs = await incidents_col().find(
        {"status": {"$in": ["resolved", "completed"]}, "resolved_at": {"$gte": now - timedelta(days=14)}},
    ).sort("resolved_at", -1).to_list(20)
    worst = max((RANK[c["state"]] for c in components), default=0)
    overall = next(s for s, r in RANK.items() if r == worst)
    text = {
        "operational": "Everything is working.",
        "maintenance": "Some parts are in planned maintenance.",
        "degraded": "Some parts are slow or partly working.",
        "down": "Some parts are not working.",
    }[overall]
    groups = []
    for c in components:
        if c["group"] not in groups:
            groups.append(c["group"])
    out = {
        "updated_at": now.isoformat(), "overall": {"state": overall, "text": text}, "groups": groups, "components": components,
        "active": active, "upcoming": upcoming, "recent": [public_incident(d) for d in recent_docs],
    }
    _cache = (time.monotonic() + CACHE_SECONDS, out)
    return out


async def incident(incident_id: str) -> Optional[dict[str, Any]]:
    doc = await incidents_col().find_one({"_id": incident_id})
    return public_incident(doc, full=True) if doc else None


async def history(page: int = 1, per_page: int = 20) -> dict[str, Any]:
    rows = await (
        incidents_col().find({"status": {"$in": ["resolved", "completed"]}})
        .sort([("started_at", -1), ("_id", -1)]).skip((page - 1) * per_page).limit(per_page + 1).to_list(None)
    )
    return {"items": [public_incident(d) for d in rows[:per_page]], "more": len(rows) > per_page}


async def create_indexes() -> None:
    db = _db()
    await db["status_daily"].create_index([("day", 1)])
    await incidents_col().create_index([("status", 1), ("started_at", -1)])
    await incidents_col().create_index([("resolved_at", -1)])
    await incidents_col().create_index("number", unique=True)
    await subscribers_col().create_index("token")
