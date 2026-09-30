"""Groups repeated model failures into single issues with a lifecycle.

An issue is found by its fingerprint. New events for a fixed issue reopen it; for an ignored issue they are
counted but stay quiet. Titles and causes come from catalogue.py, so wording lives in one place.

Lifecycle: open, acknowledged, fixed, ignored. An issue with no new events for a while is fixed
automatically; the quiet time depends on the kind (AUTO_FIX_AFTER_H). Those windows are proposals, not
provider facts."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.shared.llm_health import catalogue

logger = logging.getLogger(__name__)

STATUSES = ("open", "acknowledged", "fixed", "ignored")
PRIORITIES = ("critical", "high", "medium", "low")
REGRESSION_WINDOW_H = 24
SAMPLE_EVENTS = 20

# Hours without a new event before an open issue is marked fixed by itself.
AUTO_FIX_AFTER_H: dict[str, float] = {
    "rate_limit_minute": 2, "timeout": 2, "empty_response": 6, "content_blocked": 6,
    "quota_tokens": 6, "quota_daily": 6, "network_error": 6, "provider_outage": 6,
}
DEFAULT_AUTO_FIX_H = 24.0

#: kinds where two features failing the same way are separate problems
PER_FEATURE = {"bad_request", "unparseable_response", "empty_response", "context_too_long"}


def fingerprint(provider: str, model: str, error_type: str, feature: str | None) -> str:
    if error_type == "fallback_failed":
        return f"fallback_failed|{feature or 'unknown'}"
    base = f"{provider}|{model}|{error_type}"
    return f"{base}|{feature or 'unknown'}" if error_type in PER_FEATURE else base


def issue_number_label(number: int) -> str:
    return f"ISSUE-{number:04d}"


def derive_priority(error_type: str, *, failed_requests: int = 0, fallback_working: bool | None = None) -> str:
    """The catalogue's default, raised when users are clearly affected with no working fallback."""
    base = catalogue.kind_for(error_type).priority
    if error_type == "provider_outage" and fallback_working is False:
        return "high"
    if base == "medium" and fallback_working is False and failed_requests >= 10:
        return "high"
    return base


def title_for(issue: dict[str, Any]) -> str:
    kind = catalogue.kind_for(issue.get("error_type", "unknown"))
    features = issue.get("features") or []
    paths = issue.get("prompt_paths") or []
    return catalogue.render(
        kind.title, provider=issue.get("provider") or "The provider", model=issue.get("model") or "the model",
        feature=features[0] if features else "this feature", prompt_path=paths[0] if paths else None,
    )


def summary_for(issue: dict[str, Any]) -> str:
    kind = catalogue.kind_for(issue.get("error_type", "unknown"))
    return catalogue.render(kind.cause, provider=issue.get("provider") or "The provider", model=issue.get("model") or "the model")


def plan_update(existing: dict[str, Any] | None, events: list[dict[str, Any]], *, now: datetime, next_number: int | None) -> dict[str, Any]:
    """Pure: works out the change for one fingerprint. Returns {"insert": doc} or {"update": {...}, "reopened": bool}."""
    first = events[0]
    kind = first.get("error_type") or "unknown"
    features = sorted({e.get("feature") for e in events if e.get("feature")})
    paths = sorted({e.get("prompt_path") for e in events if e.get("prompt_path")})
    workspaces = sorted({e.get("workspace_id") for e in events if e.get("workspace_id")})
    ids = [e["_id"] for e in events if e.get("_id") is not None]
    last = max(e["at"] for e in events)
    version = events[-1].get("app_version")

    if existing is None:
        doc = {
            "number": next_number, "fingerprint": fingerprint(first["provider"], first["model"], kind, first.get("feature")),
            "error_type": kind, "provider": first["provider"], "model": first["model"],
            "features": features, "prompt_paths": paths, "affected_workspaces": workspaces,
            "status": "open", "priority": derive_priority(kind, failed_requests=len(events)),
            "priority_overridden": False, "first_seen": min(e["at"] for e in events), "last_seen": last,
            "event_count": len(events), "failed_request_count": sum(1 for e in events if e.get("outcome") != "fallback_success"),
            "ignored_count": 0, "fallback_outcome": "working" if any(e.get("fallback_to") for e in events) else "none",
            "owner_id": None, "notes": [], "history": [{"at": now, "by": "system", "change": "opened"}],
            "resolution": None, "reopen_count": 0, "regressed": False, "source": "auto",
            "sample_event_ids": ids[-SAMPLE_EVENTS:], "first_app_version": version, "last_app_version": version,
            "created_at": now, "updated_at": now,
        }
        doc["title"], doc["summary"] = title_for(doc), summary_for(doc)
        return {"insert": doc}

    status = existing.get("status", "open")
    update: dict[str, Any] = {"$set": {"last_seen": last, "updated_at": now, "last_app_version": version},
                              "$inc": {"event_count": len(events)}}
    if status == "ignored":
        update["$inc"] = {"event_count": len(events), "ignored_count": len(events)}
        return {"update": update, "reopened": False}
    update["$inc"]["failed_request_count"] = sum(1 for e in events if e.get("outcome") != "fallback_success")
    update["$addToSet"] = {"features": {"$each": features}, "prompt_paths": {"$each": paths}, "affected_workspaces": {"$each": workspaces}}
    update["$push"] = {"sample_event_ids": {"$each": ids, "$slice": -SAMPLE_EVENTS}}
    reopened = False
    if status == "fixed":
        reopened = True
        fixed_at = (existing.get("resolution") or {}).get("at")
        manual = (existing.get("resolution") or {}).get("by") not in (None, "system")
        recent = bool(fixed_at and manual and (now - _aware(fixed_at)) < timedelta(hours=REGRESSION_WINDOW_H))
        update["$set"]["status"] = "open"
        update["$set"]["regressed"] = recent
        update["$inc"]["reopen_count"] = 1
        update["$push"]["history"] = {"at": now, "by": "system", "change": "reopened (regressed)" if recent else "reopened"}
    if not existing.get("priority_overridden"):
        update["$set"]["priority"] = derive_priority(kind, failed_requests=int(existing.get("failed_request_count", 0)) + len(events))
    return {"update": update, "reopened": reopened}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_due_for_auto_fix(issue: dict[str, Any], now: datetime) -> bool:
    if issue.get("status") not in ("open", "acknowledged") or issue.get("source") == "manual":
        return False
    hours = AUTO_FIX_AFTER_H.get(issue.get("error_type", ""), DEFAULT_AUTO_FIX_H)
    return (now - _aware(issue["last_seen"])) >= timedelta(hours=hours)


async def _next_number() -> int:
    from app.db.mongo import llm_counters

    doc = await llm_counters.find_one_and_update({"_id": "issue_number"}, {"$inc": {"seq": 1}}, upsert=True, return_document=True)
    return int(doc["seq"])


def manual_issue_doc(*, number: int, title: str, detail: str, priority: str, feature: str | None, by: str | None,
                     now: datetime, kind: str = "issue", created_at: datetime | None = None,
                     legacy_note_id: str | None = None, resolved_at: datetime | None = None) -> dict[str, Any]:
    """A problem a person logged by hand. It has no events; it keeps the words its author gave."""
    from app.shared.llm_health.scrub import scrub_message

    created = created_at or now
    doc: dict[str, Any] = {
        "number": number, "fingerprint": f"manual|{number}", "error_type": "unknown", "provider": None, "model": None,
        "kind": kind, "features": [feature] if feature else [], "prompt_paths": [], "affected_workspaces": [],
        "status": "fixed" if resolved_at else "open", "priority": priority, "priority_overridden": True,
        "first_seen": created, "last_seen": resolved_at or created, "event_count": 0, "failed_request_count": 0,
        "ignored_count": 0, "fallback_outcome": "none", "owner_id": None,
        "notes": [{"at": created, "by": by, "text": scrub_message(detail, 2000)}] if detail else [],
        "history": [{"at": created, "by": by, "change": "logged by hand"}],
        "resolution": {"by": by, "at": resolved_at, "note": ""} if resolved_at else None,
        "reopen_count": 0, "regressed": False, "source": "manual", "sample_event_ids": [],
        "created_at": created, "updated_at": now, "title": title.strip(), "summary": detail.strip()[:300],
    }
    if legacy_note_id:
        doc["legacy_note_id"] = legacy_note_id
    return doc


async def merge_legacy_notes() -> int:
    """One time, safe to repeat: copies the old Ops LLM notes into issues so there is a single list.
    The old collection is left as it was. Returns how many were copied."""
    from app.db.mongo import llm_issues, ops_llm_notes

    copied = 0
    now = datetime.now(timezone.utc)
    async for note in ops_llm_notes.find({}):
        nid = str(note["_id"])
        if await llm_issues.find_one({"legacy_note_id": nid}, {"_id": 1}):
            continue
        doc = manual_issue_doc(
            number=await _next_number(), title=note.get("title", "Untitled"), detail=note.get("detail", ""),
            priority=note.get("severity", "medium") if note.get("severity") in PRIORITIES else "medium", feature=None,
            by=note.get("created_by"), now=now, kind=note.get("kind", "issue"), created_at=note.get("created_at"),
            legacy_note_id=nid, resolved_at=note.get("resolved_at") if note.get("status") == "resolved" else None,
        )
        if note.get("status") == "resolved" and not doc["resolution"]:
            doc["status"], doc["resolution"] = "fixed", {"by": note.get("created_by"), "at": now, "note": ""}
        await llm_issues.insert_one(doc)
        copied += 1
    return copied


async def apply_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Groups saved events by fingerprint and creates or updates their issues. Returns counts."""
    from app.db.mongo import llm_events, llm_issues

    groups: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        kind = e.get("error_type")
        if not kind:
            continue  # slow or fallback-success events without an error kind do not open issues
        groups.setdefault(fingerprint(e["provider"], e["model"], kind, e.get("feature")), []).append(e)

    created = updated = reopened = 0
    created_docs: list[dict[str, Any]] = []
    reopened_docs: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for fp, group in groups.items():
        existing = await llm_issues.find_one({"fingerprint": fp})
        plan = plan_update(existing, group, now=now, next_number=None if existing else await _next_number())
        if "insert" in plan:
            try:
                await llm_issues.insert_one(plan["insert"])
                issue_id = plan["insert"]["_id"]
                created += 1
                created_docs.append(plan["insert"])
            except Exception:  # noqa: BLE001 - two flushes raced on a new fingerprint: update the winner instead
                existing = await llm_issues.find_one({"fingerprint": fp})
                if not existing:
                    raise
                plan = plan_update(existing, group, now=now, next_number=None)
        if "update" in plan:
            await llm_issues.update_one({"_id": existing["_id"]}, plan["update"])
            issue_id = existing["_id"]
            updated += 1
            if plan.get("reopened"):
                reopened += 1
                fresh = await llm_issues.find_one({"_id": existing["_id"]})
                if fresh:
                    reopened_docs.append(fresh)
        await llm_events.update_many({"_id": {"$in": [e["_id"] for e in group if e.get("_id") is not None]}}, {"$set": {"issue_id": issue_id}})
    return {"created": created, "updated": updated, "reopened": reopened, "created_docs": created_docs, "reopened_docs": reopened_docs}


async def auto_fix_sweep(now: datetime | None = None) -> int:
    """Marks issues fixed that have been quiet long enough. Returns how many."""
    from app.db.mongo import llm_issues

    now = now or datetime.now(timezone.utc)
    fixed = 0
    async for issue in llm_issues.find({"status": {"$in": ["open", "acknowledged"]}, "source": {"$ne": "manual"}}):
        if is_due_for_auto_fix(issue, now):
            await llm_issues.update_one(
                {"_id": issue["_id"], "last_seen": issue["last_seen"]},
                {"$set": {"status": "fixed", "resolution": {"by": "system", "at": now, "note": "No new failures for a while."}, "updated_at": now},
                 "$push": {"history": {"at": now, "by": "system", "change": "fixed automatically"}}},
            )
            fixed += 1
    return fixed
