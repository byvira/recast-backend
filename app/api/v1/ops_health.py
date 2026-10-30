"""Ops: the error log (issues and their occurrences) and the public status (parts, incidents, planned maintenance).

Platform staff can read everything and work issues and incidents: resolve, ignore, assign, note, post updates. Every change
records who made it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import users, workspaces
from app.health import errors, status

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── the error log ────────────────────────────────────────────────────────

@router.get("/errors")
@limiter.limit("120/minute")
async def list_errors(
    request: Request,
    status_: Optional[Literal["open", "resolved", "ignored"]] = Query("open", alias="status"),
    source: Optional[str] = Query(None, max_length=20),
    q: str = Query("", max_length=100),
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """Issues, the most recent first, with how often each happened in the last hour and day."""
    query: dict[str, Any] = {}
    if status_:
        query["status"] = status_
    if source in errors.SOURCES:
        query["source"] = source
    if q.strip():
        pattern = re.escape(q.strip())
        query["$or"] = [{"title": {"$regex": pattern, "$options": "i"}}, {"where": {"$regex": pattern, "$options": "i"}}]
    rows = await errors.issues_col().find(query).sort("last_seen", -1).limit(limit).to_list(limit)
    ids = [r["_id"] for r in rows]
    hour = await errors.counts_since(ids, _now() - timedelta(hours=1))
    day = await errors.counts_since(ids, _now() - timedelta(hours=24))
    items = [{**errors.public_issue(r), "last_hour": hour.get(r["_id"], 0), "last_day": day.get(r["_id"], 0),
              "spiking": errors.is_spiking(r)} for r in rows]
    totals = {s: await errors.issues_col().count_documents({"status": s}) for s in errors.STATUSES}
    new_today = await errors.issues_col().count_documents({"first_seen": {"$gte": _now() - timedelta(hours=24)}})
    return {"items": items, "totals": totals, "new_today": new_today, "sources": [{"id": k, "label": v} for k, v in errors.SOURCES.items()]}


#: The most rows one export carries.
EXPORT_MAX = 5000


async def _log_query(
    *, source: Optional[str], workspace: Optional[str], user: Optional[str], q: str, since: Optional[datetime], until: Optional[datetime],
) -> dict[str, Any]:
    """The live log's filters as one database query. `user` is an email (any case) or a member id."""
    query: dict[str, Any] = {}
    if source in errors.SOURCES:
        query["source"] = source
    if workspace:
        query["workspace_id"] = workspace.strip()
    if user and user.strip():
        who = user.strip()
        found = await users.find_one({"email": {"$regex": f"^{re.escape(who)}$", "$options": "i"}}, {"id": 1}) if "@" in who else None
        query["user_id"] = (found or {}).get("id") or who
    if q.strip():
        pattern = re.escape(q.strip())
        query["$or"] = [{"title": {"$regex": pattern, "$options": "i"}}, {"where": {"$regex": pattern, "$options": "i"}},
                        {"message": {"$regex": pattern, "$options": "i"}}, {"request_id": q.strip()}]
    if since or until:
        query["at"] = {**({"$gt": since} if since else {}), **({"$lte": until} if until else {})}
    return query


async def _log_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Log rows as Ops reads them: the member's email and the workspace's name beside their ids."""
    user_ids = list({r["user_id"] for r in rows if r.get("user_id")})
    ws_ids = list({r["workspace_id"] for r in rows if r.get("workspace_id")})
    emails = {u["id"]: u.get("email") async for u in users.find({"id": {"$in": user_ids}}, {"id": 1, "email": 1})} if user_ids else {}
    names = {w["id"]: w.get("name") async for w in workspaces.find({"id": {"$in": ws_ids}}, {"id": 1, "name": 1})} if ws_ids else {}
    out = []
    for r in rows:
        at = r["at"] if r["at"].tzinfo else r["at"].replace(tzinfo=timezone.utc)
        out.append({
            "id": str(r["_id"]), "at": at.isoformat(), "number": r.get("number"), "title": r.get("title") or "", "source": r.get("source"),
            "source_label": errors.SOURCES.get(r.get("source") or "", ""), "kind": r.get("kind"), "where": r.get("where") or "",
            "message": r.get("message") or "", "status": r.get("status"), "request_id": r.get("request_id"), "release": r.get("release"),
            "user_id": r.get("user_id"), "user_email": emails.get(r.get("user_id") or ""),
            "workspace_id": r.get("workspace_id"), "workspace_name": names.get(r.get("workspace_id") or ""),
        })
    return out


@router.get("/log")
@limiter.limit("120/minute")
async def error_log(
    request: Request,
    source: Optional[str] = Query(None, max_length=20),
    workspace: Optional[str] = Query(None, max_length=80),
    user: Optional[str] = Query(None, max_length=200),
    q: str = Query("", max_length=100),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = Query(100, ge=1, le=200),
    _: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """Every error as it happened, newest first: who it happened to, in which workspace, where, and which issue it belongs to. The
    screen asks again every few seconds with `since` set to the newest row it has, so only new rows come back."""
    query = await _log_query(source=source, workspace=workspace, user=user, q=q, since=since, until=until)
    rows = await errors.log_col().find(query).sort("at", -1).limit(limit).to_list(limit)
    return {"items": await _log_rows(rows), "sources": [{"id": k, "label": v} for k, v in errors.SOURCES.items()]}


@router.get("/log/export")
@limiter.limit("10/minute")
async def export_error_log(
    request: Request,
    format: Literal["csv", "json"] = "csv",
    source: Optional[str] = Query(None, max_length=20),
    workspace: Optional[str] = Query(None, max_length=80),
    user: Optional[str] = Query(None, max_length=200),
    q: str = Query("", max_length=100),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    _: dict = Depends(require_platform_staff),
):
    """The log with the same filters as a file, newest first, at most 5,000 rows."""
    import csv
    import io
    import json

    from fastapi import Response

    query = await _log_query(source=source, workspace=workspace, user=user, q=q, since=since, until=until)
    rows = await _log_rows(await errors.log_col().find(query).sort("at", -1).limit(EXPORT_MAX).to_list(EXPORT_MAX))
    stamp = _now().strftime("%Y%m%d-%H%M")
    if format == "json":
        body = json.dumps(rows, ensure_ascii=False, indent=1)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="ekavo-error-log-{stamp}.json"'})
    fields = ["at", "number", "title", "source_label", "kind", "where", "message", "status", "request_id", "user_email", "user_id",
              "workspace_name", "workspace_id", "release"]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        # a cell that starts with = + - @ would run as a formula in a spreadsheet, so it is written as text
        writer.writerow({k: (f"'{v}" if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in row.items()})
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="ekavo-error-log-{stamp}.csv"'})


async def _issue_by_number(number: int) -> dict[str, Any]:
    doc = await errors.issues_col().find_one({"number": number})
    if not doc:
        raise HTTPException(status_code=404, detail="That error was not found.")
    return doc


@router.get("/errors/{number}")
@limiter.limit("120/minute")
async def get_error(request: Request, number: int, _: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """One issue with its latest occurrences and the workspaces it touched."""
    doc = await _issue_by_number(number)
    events = await errors.events_col().find({"issue_id": doc["_id"]}).sort("at", -1).limit(30).to_list(30)
    ws_ids = list(dict.fromkeys(doc.get("workspaces") or []))[-20:]
    names = {w["id"]: w.get("name") async for w in workspaces.find({"id": {"$in": ws_ids}}, {"id": 1, "name": 1})}
    member_ids = list({e["user_id"] for e in events if e.get("user_id")})
    emails = {u["id"]: u.get("email") async for u in users.find({"id": {"$in": member_ids}}, {"id": 1, "email": 1})} if member_ids else {}
    hours = await errors.by_hour(doc["_id"], _now() - timedelta(hours=24))
    incident = await status.incidents_col().find_one({"_id": doc.get("incident_id")}, {"title": 1, "status": 1, "number": 1}) if doc.get("incident_id") else None
    return {
        **errors.public_issue(doc),
        "incident": {"id": incident["_id"], "number": incident.get("number"), "title": incident.get("title"), "status": incident.get("status")} if incident else None,
        "by_hour": hours,
        "affected": [{"id": w, "name": names.get(w) or "A workspace"} for w in reversed(ws_ids)],
        "events": [{
            "at": (e["at"] if e["at"].tzinfo else e["at"].replace(tzinfo=timezone.utc)).isoformat(), "message": e.get("message"),
            "stack": e.get("stack"), "where": e.get("where"), "release": e.get("release"), "workspace": names.get(e.get("workspace_id") or ""),
            "member": emails.get(e.get("user_id") or ""), "extra": e.get("extra"),
        } for e in events],
    }


class IssueChange(BaseModel):
    status: Optional[Literal["open", "resolved", "ignored"]] = None
    assigned_to: Optional[str] = Field(None, max_length=120)
    unassign: bool = False
    note: Optional[str] = Field(None, max_length=1000)


@router.patch("/errors/{number}")
@limiter.limit("60/minute")
async def change_error(request: Request, number: int, body: IssueChange, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    doc = await _issue_by_number(number)
    who = user.get("email") or user["id"]
    change: dict[str, Any] = {"$set": {}}
    if body.status:
        change["$set"]["status"] = body.status
        change["$set"][f"{body.status}_at"] = _now()
        change["$set"][f"{body.status}_by"] = who
    if body.unassign:
        change["$set"]["assigned_to"] = None
    elif body.assigned_to is not None:
        change["$set"]["assigned_to"] = body.assigned_to.strip() or None
    notes = []
    if body.status:
        notes.append({"at": _now(), "by": who, "text": {"open": "Opened again", "resolved": "Marked resolved", "ignored": "Ignored"}[body.status]})
    if body.note and body.note.strip():
        notes.append({"at": _now(), "by": who, "text": body.note.strip()})
    if notes:
        change["$push"] = {"notes": {"$each": notes, "$slice": -50}}
    if not change["$set"]:
        change.pop("$set")
    if len(change) == 0:
        raise HTTPException(status_code=400, detail="Choose a change first.")
    await errors.issues_col().update_one({"_id": doc["_id"]}, change)
    if body.assigned_to and body.assigned_to.strip() and body.assigned_to.strip() != doc.get("assigned_to") and body.assigned_to.strip() != user.get("email"):
        await errors.notify_assignee(doc, body.assigned_to.strip(), who)
    return await get_error(request, number, user)


@router.get("/staff")
@limiter.limit("30/minute")
async def staff_list(request: Request, _: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """The people an issue can be given to."""
    rows = await users.find({"$or": [{"is_platform_staff": True}, {"is_master_admin": True}]}, {"email": 1, "name": 1}).to_list(100)
    return {"staff": [{"email": r.get("email"), "name": r.get("name") or r.get("email")} for r in rows if r.get("email")]}


# ── the public status ────────────────────────────────────────────────────

@router.get("/status")
@limiter.limit("120/minute")
async def ops_status(request: Request, _: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """What the public page shows, plus every open incident with all its updates and who wrote them."""
    status.forget()
    summary = await status.summary()
    open_docs = await status.incidents_col().find({"status": {"$nin": ["resolved", "completed"]}}).sort("started_at", -1).to_list(50)
    recent = await status.incidents_col().find({"status": {"$in": ["resolved", "completed"]}}).sort("started_at", -1).limit(20).to_list(20)

    def staff_view(d: dict) -> dict:
        out = status.public_incident(d, full=True)
        out["auto"] = bool(d.get("auto"))
        out["error_numbers"] = d.get("error_numbers") or []
        out["updates"] = [
            {**u, "by": src.get("by")} for u, src in zip(out["updates"], reversed(d.get("updates") or []))
        ]
        return out

    return {
        **summary,
        "parts": [{"id": c["id"], "label": c["label"], "group": c["group"]} for c in status.COMPONENTS],
        "open": [staff_view(d) for d in open_docs],
        "closed": [staff_view(d) for d in recent],
    }


Impact = Literal["degraded", "down", "maintenance"]
IncidentStatus = Literal["investigating", "identified", "monitoring", "resolved", "scheduled", "in_progress", "completed"]


class NewIncident(BaseModel):
    kind: Literal["incident", "maintenance"] = "incident"
    title: str = Field(..., min_length=3, max_length=160)
    components: list[str] = Field(..., min_length=1, max_length=20)
    impact: Impact = "degraded"
    text: str = Field(..., min_length=3, max_length=2000)
    scheduled_for: Optional[datetime] = None
    scheduled_until: Optional[datetime] = None
    #: the error in the Ops error log this notice is about, so the two are linked both ways
    error_number: Optional[int] = Field(None, ge=1)


def _parts(ids: list[str]) -> list[str]:
    unknown = [c for c in ids if c not in status.BY_ID]
    if unknown:
        raise HTTPException(status_code=400, detail="Choose parts from the list.")
    return list(dict.fromkeys(ids))


def _aware(v: Optional[datetime]) -> Optional[datetime]:
    return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)) if v else None


@router.post("/incidents", status_code=201)
@limiter.limit("30/minute")
async def create_incident(request: Request, body: NewIncident, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Post an incident the public can see, or plan maintenance ahead."""
    parts = _parts(body.components)
    if body.kind == "maintenance":
        start, end = _aware(body.scheduled_for), _aware(body.scheduled_until)
        if not start or not end or end <= start:
            raise HTTPException(status_code=400, detail="Maintenance needs a start and an end after it.")
        doc = await status.open_incident(
            kind="maintenance", title=body.title, components=parts, impact="maintenance", text=body.text, by=user.get("email"),
            status="scheduled", started_at=start, scheduled_for=start, scheduled_until=end,
        )
    else:
        doc = await status.open_incident(title=body.title, components=parts, impact=body.impact, text=body.text, by=user.get("email"),
                                         error_numbers=[body.error_number] if body.error_number else None)
    await status.incidents_col().update_one({"_id": doc["_id"]}, {"$set": {"staff_touched": True}})
    return status.public_incident(doc, full=True)


class IncidentUpdate(BaseModel):
    status: IncidentStatus
    text: str = Field(..., min_length=3, max_length=2000)
    impact: Optional[Impact] = None


@router.post("/incidents/{incident_id}/updates")
@limiter.limit("60/minute")
async def update_incident(request: Request, incident_id: str, body: IncidentUpdate, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """Add an update the public sees. Resolved (or completed, for maintenance) closes it."""
    current = await status.incidents_col().find_one({"_id": incident_id[:64]})
    if not current:
        raise HTTPException(status_code=404, detail="That incident was not found.")
    maintenance = current.get("kind") == "maintenance"
    allowed = {"scheduled", "in_progress", "completed"} if maintenance else {"investigating", "identified", "monitoring", "resolved"}
    if body.status not in allowed:
        raise HTTPException(status_code=400, detail="That step does not fit this kind of notice.")
    if body.impact and not maintenance:
        await status.incidents_col().update_one({"_id": current["_id"]}, {"$set": {"impact": body.impact}})
    doc = await status.add_update(current["_id"], status=body.status, text=body.text, by=user.get("email") or user["id"])
    return status.public_incident(doc, full=True)


class Writeup(BaseModel):
    writeup: str = Field("", max_length=5000)


@router.patch("/incidents/{incident_id}")
@limiter.limit("30/minute")
async def write_up(request: Request, incident_id: str, body: Writeup, user: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """What happened and what was changed, shown on the notice's public page once it is closed."""
    current = await status.incidents_col().find_one({"_id": incident_id[:64]}, {"status": 1})
    if not current:
        raise HTTPException(status_code=404, detail="That incident was not found.")
    if current.get("status") not in ("resolved", "completed"):
        raise HTTPException(status_code=400, detail="Write this up once the notice is closed.")
    doc = await status.set_writeup(current["_id"], body.writeup, user.get("email") or user["id"])
    return status.public_incident(doc, full=True)
