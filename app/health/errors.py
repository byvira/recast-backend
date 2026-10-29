"""The error log: every unexpected error, grouped into issues Ops can follow, resolve or ignore.

An error is grouped by its fingerprint (where it came from, what kind of error, the place it happened with ids taken out, and
the first line of our own code in its trace), so a thousand of the same failure are one issue with a count. A resolved issue
that happens again reopens by itself; an ignored one is counted but stays quiet.

Nothing private is kept: messages and traces go through the same scrubber as the AI call log (no keys, tokens, emails or long
ids), and single occurrences are kept 30 days. `capture` never raises and never waits on the caller: an error in the error log
must not become a second error.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.mongo import get_db

logger = logging.getLogger(__name__)

SOURCES: dict[str, str] = {
    "server": "Server",
    "job": "Background work",
    "scheduler": "Scheduled job",
    "browser": "Member's screen",
    "worker": "Render machine",
    "logged": "Caught and logged",
}
STATUSES = ("open", "resolved", "ignored")
KEEP_EVENTS_DAYS = 30
#: after this many stored occurrences an issue keeps every tenth, so one storm cannot fill the log
FULL_EVENTS = 50
SPIKE_COUNT = 20
SPIKE_MINUTES = 5
ALERT_COOLDOWN = timedelta(minutes=60)

_tasks: set[asyncio.Task] = set()


def issues_col():
    return get_db()["error_issues"]


def events_col():
    return get_db()["error_events"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def release() -> str:
    import os

    return (os.environ.get("RENDER_GIT_COMMIT") or "local")[:12]


_IDS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), ":id"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), ":id"),
    (re.compile(r"\b\d{2,}\b"), ":n"),
]


def normalise_where(where: str) -> str:
    """A route or page with its ids taken out, so /posts/abc123 and /posts/def456 are the same place."""
    out = (where or "").split("?")[0].strip()
    for pattern, repl in _IDS:
        out = pattern.sub(repl, out)
    return out[:200] or "unknown"


def _our_frame(stack: str) -> str:
    """The deepest line of Ekavo's own code in a trace, which says where the failure really is."""
    lines = [ln.strip() for ln in (stack or "").splitlines() if ("app/" in ln or "app\\" in ln or "/components/" in ln or "/lib/" in ln)]
    frame = lines[-1] if lines else ""
    return re.sub(r"line \d+", "line", frame)[:200]


def fingerprint(source: str, kind: str, where: str, stack: str) -> str:
    raw = "|".join([source, kind or "Error", normalise_where(where), _our_frame(stack)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _scrub(text: str, limit: int) -> str:
    from app.shared.llm_health.scrub import scrub_message

    return scrub_message(text, limit)


def _scrub_stack(stack: str) -> str:
    from app.shared.llm_health.scrub import _PATTERNS

    out = (stack or "")[-8000:]
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def title_for(kind: str, message: str) -> str:
    first = (message or "").splitlines()[0] if message else ""
    return f"{kind}: {first}"[:160] if first else kind[:160]


async def _next_number() -> int:
    doc = await get_db()["error_counters"].find_one_and_update(
        {"_id": "issues"}, {"$inc": {"n": 1}}, upsert=True, return_document=True,
    )
    return int(doc["n"])


async def _record(
    *, source: str, kind: str, message: str, where: str, stack: str, workspace_id: Optional[str], user_id: Optional[str],
    extra: Optional[dict[str, Any]], rel: Optional[str],
) -> Optional[dict[str, Any]]:
    now = _now()
    kind = (kind or "Error")[:80]
    message = _scrub(message, 500)
    stack = _scrub_stack(stack)
    fp = fingerprint(source, kind, where, stack)
    rel = (rel or release())[:40]
    place = normalise_where(where)
    existing = await issues_col().find_one({"_id": fp}, {"status": 1, "count": 1, "number": 1})
    if existing is None:
        try:
            number = await _next_number()
            await issues_col().insert_one({
                "_id": fp, "number": number, "source": source, "kind": kind, "title": title_for(kind, message), "where": place,
                "status": "open", "count": 0, "first_seen": now, "last_seen": now, "first_release": rel, "releases": [],
                "workspaces": [], "users": [], "assigned_to": None, "notes": [], "frame": _our_frame(stack),
            })
        except Exception:  # noqa: BLE001 - two of the same error at once: the other one made the issue
            existing = await issues_col().find_one({"_id": fp}, {"status": 1, "count": 1, "number": 1})
    change: dict[str, Any] = {
        "$inc": {"count": 1},
        "$set": {"last_seen": now, "last_message": message, "last_release": rel},
        "$addToSet": {"releases": rel},
    }
    if workspace_id:
        change["$addToSet"]["workspaces"] = workspace_id
    if user_id:
        change["$addToSet"]["users"] = user_id
    reopened = bool(existing and existing.get("status") == "resolved")
    if reopened:
        change["$set"].update({"status": "open", "reopened_at": now})
    after = await issues_col().find_one_and_update({"_id": fp}, change, return_document=True)
    if after is None:
        return None
    # the lists of who was touched stay bounded; the count keeps the full story
    if len(after.get("workspaces") or []) > 200 or len(after.get("users") or []) > 200:
        await issues_col().update_one({"_id": fp}, {"$push": {"workspaces": {"$each": [], "$slice": -200}, "users": {"$each": [], "$slice": -200}}})
    # Every occurrence is counted per hour (what the list, the chart and "last hour" read), even when not every one is stored, and
    # a burst count over the last few minutes drives the "happening a lot" alert.
    hour = now.replace(minute=0, second=0, microsecond=0)
    await counts_col().update_one({"_id": f"{fp}:{hour:%Y%m%d%H}"}, {"$inc": {"n": 1}, "$setOnInsert": {"issue_id": fp, "hour": hour}}, upsert=True)
    burst_start = after.get("burst_start")
    burst_start = burst_start.replace(tzinfo=timezone.utc) if isinstance(burst_start, datetime) and burst_start.tzinfo is None else burst_start
    if not burst_start or now - burst_start > timedelta(minutes=SPIKE_MINUTES):
        await issues_col().update_one({"_id": fp}, {"$set": {"burst_start": now, "burst_count": 1}})
        after["burst_count"] = 1
    else:
        burst = await issues_col().find_one_and_update({"_id": fp}, {"$inc": {"burst_count": 1}}, {"burst_count": 1}, return_document=True)
        after["burst_count"] = int((burst or {}).get("burst_count") or 0)
    count = int(after.get("count") or 0)
    if count <= FULL_EVENTS or count % 10 == 0:
        await events_col().insert_one({
            "issue_id": fp, "at": now, "message": message, "stack": stack, "where": (where or "")[:300], "workspace_id": workspace_id,
            "user_id": user_id, "release": rel, "extra": {k: str(v)[:300] for k, v in (extra or {}).items()} or None,
        })
    return {**after, "_new": existing is None, "_reopened": reopened}


#: New caught-and-logged errors alert at most this many times an hour: the first deploy finds many old ones at once, and every one
#: is still recorded in the log for Ops to read.
LOGGED_ALERTS_PER_HOUR = 10
_logged_alerts: dict[str, int] = {}


def _logged_alert_allowed() -> bool:
    hour = _now().strftime("%Y%m%d%H")
    for key in [k for k in _logged_alerts if k != hour]:
        _logged_alerts.pop(key, None)
    _logged_alerts[hour] = _logged_alerts.get(hour, 0) + 1
    return _logged_alerts[hour] <= LOGGED_ALERTS_PER_HOUR


async def _alert(issue: dict[str, Any]) -> None:
    """Tell Ops about a new issue, one that came back, or one happening many times at once. Each kind of alert for an issue is
    sent at most once an hour, through the same email and Slack settings as the AI health alerts."""
    from app.core.config import settings
    from app.shared.llm_health import alerts
    from app.shared.llm_health.service import get_alert_rules

    number = issue.get("number")
    link = f"{(settings.FRONTEND_URL or '').rstrip('/')}/ops/errors?issue={number}"
    candidates: list[alerts.Alert] = []
    label = SOURCES.get(issue.get("source", ""), "Ekavo")
    if issue.get("_new") and issue.get("source") == "logged" and not _logged_alert_allowed():
        return
    if issue.get("_new"):
        candidates.append(alerts.Alert(
            key=f"error:new:{issue['_id']}", kind="new_issue", severity="high",
            subject=f"New error #{number} ({label}): {issue.get('title', '')[:90]}",
            body=f"First seen {issue.get('where', '')}.\nRelease {issue.get('last_release', '')}.\n{link}",
        ))
    elif issue.get("_reopened"):
        candidates.append(alerts.Alert(
            key=f"error:reopened:{issue['_id']}", kind="reopened", severity="high",
            subject=f"Error #{number} is back after it was resolved: {issue.get('title', '')[:80]}",
            body=f"It happened again in release {issue.get('last_release', '')}.\n{link}",
        ))
    else:
        recent = int(issue.get("burst_count") or 0)
        if recent >= SPIKE_COUNT:
            candidates.append(alerts.Alert(
                key=f"error:spike:{issue['_id']}", kind="spike", severity="critical",
                subject=f"Error #{number} is happening many times: {issue.get('title', '')[:80]}",
                body=f"{recent} times in the last {SPIKE_MINUTES} minutes.\n{link}",
            ))
    if not candidates:
        return
    sent = await alerts.recently_sent([a.key for a in candidates], ALERT_COOLDOWN)
    rules = await get_alert_rules()
    for alert in candidates:
        if alert.key not in sent:
            await alerts.deliver(alert, rules)


async def _capture(**kw: Any) -> None:
    try:
        issue = await _record(**kw)
        if issue and issue.get("status") != "ignored":
            await _alert(issue)
    except Exception as exc:  # noqa: BLE001 - the error log must never raise
        logger.warning("Could not record an error in the error log: %s", exc)


def capture(
    *, source: str, kind: str, message: str, where: str = "", stack: str = "", workspace_id: Optional[str] = None,
    user_id: Optional[str] = None, extra: Optional[dict[str, Any]] = None, rel: Optional[str] = None,
) -> None:
    """Record one error in the background. Safe to call from anywhere, including an error handler."""
    coro = _capture(
        source=source if source in SOURCES else "server", kind=kind, message=message, where=where, stack=stack,
        workspace_id=workspace_id, user_id=user_id, extra=extra, rel=rel,
    )
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def capture_exception(exc: BaseException, *, source: str, where: str, workspace_id: Optional[str] = None,
                      user_id: Optional[str] = None, extra: Optional[dict[str, Any]] = None) -> None:
    capture(
        source=source, kind=type(exc).__name__, message=str(exc), where=where,
        stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        workspace_id=workspace_id, user_id=user_id, extra=extra,
    )


def is_expected(exc: BaseException) -> bool:
    """A refusal in plain words (a limit, a missing item, a wrong input) is an answer, not an error to log."""
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        return exc.status_code < 500
    return type(exc).__name__ in ("RunCancelled", "CancelledError", "CreditsExhausted")


# ── for Ops ──────────────────────────────────────────────────────────────

def public_issue(doc: dict[str, Any]) -> dict[str, Any]:
    def iso(v: Any) -> Optional[str]:
        if not isinstance(v, datetime):
            return None
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()

    return {
        "id": doc["_id"], "number": doc.get("number"), "source": doc.get("source"), "source_label": SOURCES.get(doc.get("source", ""), ""),
        "kind": doc.get("kind"), "title": doc.get("title"), "where": doc.get("where"), "status": doc.get("status"),
        "count": int(doc.get("count") or 0), "first_seen": iso(doc.get("first_seen")), "last_seen": iso(doc.get("last_seen")),
        "first_release": doc.get("first_release"), "last_release": doc.get("last_release"), "releases": (doc.get("releases") or [])[-10:],
        "workspaces": len(doc.get("workspaces") or []), "users": len(doc.get("users") or []), "assigned_to": doc.get("assigned_to"),
        "last_message": doc.get("last_message"), "reopened_at": iso(doc.get("reopened_at")), "frame": doc.get("frame"),
        "notes": [{**n, "at": iso(n.get("at"))} for n in (doc.get("notes") or [])][-20:],
        "tickets": (doc.get("tickets") or [])[-20:], "incident_id": doc.get("incident_id"),
    }


def counts_col():
    return get_db()["error_counts"]


async def counts_since(issue_ids: list[str], since: datetime) -> dict[str, int]:
    """How many times each issue happened since `since`, to the hour (every occurrence, not only the stored ones)."""
    out: dict[str, int] = {}
    hour = since.replace(minute=0, second=0, microsecond=0)
    async for row in counts_col().aggregate([
        {"$match": {"issue_id": {"$in": issue_ids}, "hour": {"$gte": hour}}}, {"$group": {"_id": "$issue_id", "n": {"$sum": "$n"}}},
    ]):
        out[row["_id"]] = int(row["n"])
    return out


async def by_hour(issue_id: str, since: datetime) -> list[dict[str, Any]]:
    rows = await counts_col().find({"issue_id": issue_id, "hour": {"$gte": since.replace(minute=0, second=0, microsecond=0)}}).sort("hour", 1).to_list(48)
    out = []
    for r in rows:
        at = r["hour"] if r["hour"].tzinfo else r["hour"].replace(tzinfo=timezone.utc)
        out.append({"hour": at.isoformat(), "count": int(r["n"])})
    return out


def is_spiking(issue: dict[str, Any]) -> bool:
    start = issue.get("burst_start")
    if not isinstance(start, datetime):
        return False
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    return int(issue.get("burst_count") or 0) >= SPIKE_COUNT and _now() - start <= timedelta(minutes=15)


async def create_indexes() -> None:
    await issues_col().create_index("number", unique=True)
    await issues_col().create_index([("status", 1), ("last_seen", -1)])
    await issues_col().create_index([("source", 1), ("last_seen", -1)])
    await events_col().create_index([("issue_id", 1), ("at", -1)])
    await events_col().create_index("at", expireAfterSeconds=KEEP_EVENTS_DAYS * 24 * 3600)
    await counts_col().create_index([("issue_id", 1), ("hour", 1)])
    await counts_col().create_index("hour", expireAfterSeconds=KEEP_EVENTS_DAYS * 24 * 3600)


# ── links to support tickets and status notices ─────────────────────────

REF_PATTERN = re.compile(r"\bCR-[A-Z0-9]{8}\b")


async def link_ticket(text: str, ticket: dict[str, Any]) -> int:
    """A support ticket that quotes a crash reference ("CR-..." from the crash screen) is listed on that error's issue, so Ops sees
    who reported it. Returns how many issues were linked."""
    linked = 0
    for ref in list(dict.fromkeys(REF_PATTERN.findall(text or "")))[:5]:
        event = await events_col().find_one({"extra.ref": ref}, {"issue_id": 1})
        if not event:
            continue
        row = {"id": ticket.get("id"), "number": ticket.get("number"), "subject": (ticket.get("subject") or "")[:120], "at": _now()}
        await issues_col().update_one({"_id": event["issue_id"]}, {"$push": {"tickets": {"$each": [row], "$slice": -50}}})
        linked += 1
    return linked


async def notify_assignee(issue: dict[str, Any], email: str, by: str) -> None:
    """Tell the person an error was given to, by email. Never raises."""
    try:
        from app.core.config import settings
        from app.shared.llm_health.alerts import _send_email

        link = f"{(settings.FRONTEND_URL or '').rstrip('/')}/ops/errors?issue={issue.get('number')}"
        await _send_email([email], f"Error #{issue.get('number')} was given to you", f"{by} gave you: {issue.get('title', '')}\n{link}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not email the person an error was given to: %s", exc)


# ── errors the code catches and only logs ───────────────────────────────

#: loggers whose errors are already captured where they happen, or that would capture their own failures
_SKIP_LOGGERS = ("app.health", "app.core.errors", "app.shared.pipeline_runs", "app.workers.jobs", "app.shared.llm_health", "uvicorn")


class LogCapture(logging.Handler):
    """Sends every error the code logs (a failure caught so the work could carry on) to the error log. Errors logged in a worker
    thread are handed to the server's event loop. A record from the error log itself is never captured, so it cannot loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__(level=logging.ERROR)
        self.loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_SKIP_LOGGERS) or getattr(record, "_from_error_log", False):
                return
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                kind, message = type(exc).__name__, f"{record.getMessage()}: {exc}"
                stack = "".join(traceback.format_exception(*record.exc_info))
            else:
                kind, message, stack = "Logged error", record.getMessage(), ""
            # the place is the logger and function, plus the message as written (before its values), so one log line is one issue
            where = f"{record.name}.{record.funcName}: {str(record.msg)[:80]}"
            kwargs = dict(source="logged", kind=kind, message=message, where=where, stack=stack or f'File "app/{record.pathname[-60:]}", line {record.lineno}')
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is self.loop:
                capture(**kwargs)
            elif self.loop.is_running():
                self.loop.call_soon_threadsafe(lambda: capture(**kwargs))
        except Exception:  # noqa: BLE001 - logging must never fail because of the error log
            pass


_installed: Optional[LogCapture] = None
#: the root logger, and "app", which keeps its lines to itself (propagate is off), so its errors would otherwise never reach the hook
_CAPTURED_LOGGERS = ("", "app")


def install_log_capture() -> None:
    """Called once at start-up, from inside the server's event loop."""
    global _installed
    if _installed is not None:
        # the server started again in the same process (a reload): the hook follows the new event loop
        _installed.loop = asyncio.get_running_loop()
        return
    _installed = LogCapture(asyncio.get_running_loop())
    for name in _CAPTURED_LOGGERS:
        logging.getLogger(name).addHandler(_installed)


def uninstall_log_capture() -> None:
    global _installed
    if _installed is not None:
        for name in _CAPTURED_LOGGERS:
            logging.getLogger(name).removeHandler(_installed)
        _installed = None
