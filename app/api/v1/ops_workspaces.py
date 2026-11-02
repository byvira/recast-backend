"""Ops, Workspaces: every customer workspace in one place, for support, quality audits and spotting problems.

A list to search and group (personal or work, plan, flagged, payment trouble, quiet for 30 days), then one workspace's whole picture:
overview, brand profiles, members and their roles, content with its quality and safety marks, billing (payments, receipts, refunds,
credit packs, credits), Ops notes and flags, and recent activity.

Staff can look, add notes and flags, and change a member's role, each with a reason, all kept in the workspace's Ops history. Nobody
in Ops edits or deletes a customer's content: it stays theirs. The whole group is guarded by the "workspaces" area where it is
mounted (view to read, edit to change).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from fastapi.responses import Response

from app.core.auth import get_current_user
from app.core.exports import csv_response
from app.core.middleware import limiter
from app.core.staff import require_action
from app.db.mongo import get_db, users, workspace_members, workspaces
from app.shared.brand_name import brand_display_name

router = APIRouter()
# the person view: the same area, from a person instead of a workspace
people_router = APIRouter()

QUIET_AFTER = timedelta(days=30)
PAGE = 50
# the "quiet for 30 days" filter looks through this many workspaces at most for a page
QUIET_SCAN = 500
# the most rows one export writes
EXPORT_MAX = 10_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def notes_col():
    return get_db()["ops_workspace_notes"]


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _workspace(workspace_id: str) -> dict:
    ws = await workspaces.find_one({"id": workspace_id[:80]}, {"_id": 0})
    if not ws:
        raise HTTPException(status_code=404, detail="That workspace doesn't exist.")
    return ws


async def _last_active(ids: list[str]) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    if not ids:
        return out
    async for row in get_db()["activity_entries"].aggregate([
        {"$match": {"workspace_id": {"$in": ids}}}, {"$group": {"_id": "$workspace_id", "at": {"$max": "$occurred_at"}}},
    ]):
        when = _aware(row.get("at"))
        if when:
            out[row["_id"]] = when
    return out


def _brand_name(b: dict) -> str:
    return brand_display_name(b, "Unnamed brand")


# ── the list ─────────────────────────────────────────────────────────────

class ListFilters:
    """The list's search and filters, read from the address, shared by the page and its export."""

    def __init__(
        self,
        q: Optional[str] = Query(None, max_length=100),
        purpose: Optional[Literal["personal", "work"]] = None,
        plan: Optional[Literal["free", "single", "duo", "large"]] = None,
        flagged: bool = False,
        payment_trouble: bool = False,
        quiet: bool = False,
    ) -> None:
        self.q, self.purpose, self.plan, self.flagged, self.payment_trouble, self.quiet = q, purpose, plan, flagged, payment_trouble, quiet


@router.get("")
@limiter.limit("60/minute")
async def list_workspaces(request: Request, f: ListFilters = Depends(), page: int = Query(0, ge=0, le=200)) -> dict:
    rows, active = await _matching(f, skip=page * PAGE, limit=PAGE)
    return {"workspaces": await _rows_out(rows, active), "page": page, "more": len(rows) == PAGE}


async def _matching(f: ListFilters, *, skip: int, limit: int) -> tuple[list[dict], dict[str, datetime]]:
    q, purpose, plan, flagged, payment_trouble, quiet = f.q, f.purpose, f.plan, f.flagged, f.payment_trouble, f.quiet
    query: dict[str, Any] = {"deleted_at": {"$exists": False}}
    if q and q.strip():
        term = re.escape(q.strip())
        owners = [u["id"] async for u in users.find({"email": {"$regex": term, "$options": "i"}}, {"id": 1}).limit(50)]
        query["$or"] = [{"id": q.strip()}, {"name": {"$regex": term, "$options": "i"}}, {"owner_id": {"$in": owners}}]
    if purpose:
        query["purpose"] = purpose
    if plan:
        query["plan"] = plan if plan != "free" else {"$in": [None, "free"]}
    if payment_trouble:
        query["billing_status"] = {"$in": ["past_due", "limited"]}
    if flagged:
        query["id"] = {"$in": await notes_col().distinct("workspace_id", {"flag": True, "open": True})}
    # "quiet" needs each workspace's last activity, so it looks through a bounded window and pages what is left
    scan = max(limit, QUIET_SCAN) if quiet else limit
    rows = await workspaces.find(query, {"_id": 0}).sort("created_at", -1).skip(0 if quiet else skip).limit(scan).to_list(scan)
    active = await _last_active([r["id"] for r in rows])
    if quiet:
        cutoff = _now() - QUIET_AFTER
        rows = [r for r in rows if (active.get(r["id"]) or _aware(r.get("created_at")) or _now()) < cutoff][skip:skip + limit]
    return rows, active


async def _rows_out(rows: list[dict], active: dict[str, datetime]) -> list[dict]:
    ids = [r["id"] for r in rows]
    owners = {u["id"]: u async for u in users.find({"id": {"$in": [r.get("owner_id") for r in rows]}}, {"id": 1, "email": 1, "name": 1})}
    members: dict[str, int] = {}
    async for m in workspace_members.aggregate([
        {"$match": {"workspace_id": {"$in": ids}, "status": "active"}}, {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}},
    ]):
        members[m["_id"]] = int(m["n"])
    flags: dict[str, int] = {}
    async for f in notes_col().aggregate([
        {"$match": {"workspace_id": {"$in": ids}, "flag": True, "open": True}}, {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}},
    ]):
        flags[f["_id"]] = int(f["n"])
    return [{
        "id": r["id"], "name": r.get("name") or "", "purpose": r.get("purpose"), "plan": r.get("plan") or "free",
        "billing_status": r.get("billing_status") or "none", "owner_email": (owners.get(r.get("owner_id")) or {}).get("email"),
        "members": members.get(r["id"], 0), "open_flags": flags.get(r["id"], 0), "created_at": r.get("created_at"),
        "last_active": active.get(r["id"]), "setup_pending": bool(r.get("setup_pending")),
    } for r in rows]


@router.get("/export.csv")
@limiter.limit("10/minute")
async def export_list(request: Request, f: ListFilters = Depends(), staff: dict = Depends(require_action("workspaces", "approve"))) -> Response:
    """The list as it is filtered, as a spreadsheet file (up to EXPORT_MAX rows). Holds customer emails, so it needs Approve."""
    rows, active = await _matching(f, skip=0, limit=EXPORT_MAX)
    out = await _rows_out(rows, active)
    await _log_export(staff, "workspaces", len(out))
    return csv_response("ekavo-workspaces", ["Workspace", "Id", "Personal or work", "Plan", "Payment", "Owner email", "Members", "Open flags",
                                              "Created", "Last active"],
                        [[r["name"], r["id"], r["purpose"] or "", r["plan"], r["billing_status"], r["owner_email"] or "", r["members"],
                          r["open_flags"], r["created_at"], r["last_active"]] for r in out])


# ── one workspace ────────────────────────────────────────────────────────

@router.get("/{workspace_id}")
@limiter.limit("120/minute")
async def overview(request: Request, workspace_id: str) -> dict:
    from app.shared import entitlements

    ws = await _workspace(workspace_id)
    owner = await users.find_one({"id": ws.get("owner_id")}, {"_id": 0, "id": 1, "name": 1, "email": 1}) or {}
    ent = await entitlements.entitlements_for(workspace_id)
    db = get_db()
    connections = [c async for c in db["workspace_connections"].find(
        {"workspace_id": workspace_id, "is_active": True}, {"_id": 0, "platform": 1, "account_name": 1, "username": 1})]
    counts = {
        "members": await workspace_members.count_documents({"workspace_id": workspace_id, "status": "active"}),
        "brands": await db["brand_profiles"].count_documents({"workspace_id": workspace_id}),
        "posts": await db["content_pieces"].count_documents({"workspace_id": workspace_id, "deleted": {"$ne": True}}),
        "published": await db["content_pieces"].count_documents({"workspace_id": workspace_id, "publish_status": "published"}),
        "flagged_posts": await db["content_pieces"].count_documents({"workspace_id": workspace_id, "flagged_for_review": True, "deleted": {"$ne": True}}),
        "safety_events": await db["safety_events"].count_documents({"workspace_id": workspace_id}),
        "open_flags": await notes_col().count_documents({"workspace_id": workspace_id, "flag": True, "open": True}),
    }
    return {
        "id": ws["id"], "name": ws.get("name"), "purpose": ws.get("purpose"), "tier": ws.get("tier"), "language": ws.get("language"),
        "created_at": ws.get("created_at"), "setup_pending": bool(ws.get("setup_pending")), "billing_group_id": ws.get("billing_group_id"),
        "owner": owner, "plan": ent.plan, "plan_name": ent.plan_name, "seats": ent.seats_billed, "billing_status": ws.get("billing_status") or "none",
        "connections": connections, "counts": counts, "last_active": (await _last_active([workspace_id])).get(workspace_id),
    }


@router.get("/{workspace_id}/brands")
@limiter.limit("60/minute")
async def brands(request: Request, workspace_id: str) -> dict:
    await _workspace(workspace_id)
    db = get_db()
    rows = await db["brand_profiles"].find({"workspace_id": workspace_id}, {"_id": 0}).sort("created_at", 1).to_list(100)
    used: dict[str, int] = {}
    async for row in db["content_pieces"].aggregate([
        {"$match": {"workspace_id": workspace_id, "deleted": {"$ne": True}}}, {"$group": {"_id": "$brand_id", "n": {"$sum": 1}}},
    ]):
        used[str(row["_id"])] = int(row["n"])
    return {"brands": [{
        "id": b["id"], "name": _brand_name(b), "type": b.get("brand_type"), "is_default": bool(b.get("is_default")),
        "is_active": b.get("is_active", True), "is_complete": bool(b.get("is_complete")), "language": b.get("language"),
        "platforms": b.get("platforms") or [], "posts": used.get(b["id"], 0), "created_at": b.get("created_at"),
    } for b in rows]}


@router.get("/{workspace_id}/members")
@limiter.limit("60/minute")
async def members(request: Request, workspace_id: str) -> dict:
    from app.core.rbac import ROLE_PERMISSIONS

    await _workspace(workspace_id)
    rows = await workspace_members.find({"workspace_id": workspace_id}, {"_id": 0}).sort("joined_at", 1).to_list(500)
    people = {u["id"]: u async for u in users.find({"id": {"$in": [r["user_id"] for r in rows]}}, {"id": 1, "name": 1, "email": 1, "last_active": 1})}
    posts: dict[str, int] = {}
    async for row in get_db()["content_pieces"].aggregate([
        {"$match": {"workspace_id": workspace_id, "deleted": {"$ne": True}}}, {"$group": {"_id": "$user_id", "n": {"$sum": 1}}},
    ]):
        posts[str(row["_id"])] = int(row["n"])
    return {"members": [{
        "user_id": r["user_id"], "name": (people.get(r["user_id"]) or {}).get("name") or "", "email": (people.get(r["user_id"]) or {}).get("email") or "",
        "role": r.get("role"), "status": r.get("status"), "joined_at": r.get("joined_at"), "last_active": (people.get(r["user_id"]) or {}).get("last_active"),
        "posts": posts.get(r["user_id"], 0), "can": sorted(ROLE_PERMISSIONS.get(str(r.get("role")), set())),
    } for r in rows]}


class RoleIn(BaseModel):
    role: Literal["admin", "editor", "viewer"]
    reason: str = Field(..., min_length=3, max_length=300)


@router.put("/{workspace_id}/members/{user_id}/role")
@limiter.limit("20/minute")
async def change_role(request: Request, workspace_id: str, user_id: str, body: RoleIn, staff: dict = Depends(get_current_user)) -> dict:
    """Ops changes a member's role, for example when an owner asks support to. The owner's role never changes here."""
    await _workspace(workspace_id)
    member = await workspace_members.find_one({"workspace_id": workspace_id, "user_id": user_id, "status": "active"})
    if not member:
        raise HTTPException(status_code=404, detail="That person isn't a member of this workspace.")
    if member.get("role") == "owner":
        raise HTTPException(status_code=400, detail="The owner's role can't be changed here.")
    if member.get("role") == body.role:
        raise HTTPException(status_code=400, detail="They already have that role.")
    await workspace_members.update_one({"_id": member["_id"]}, {"$set": {"role": body.role, "updated_at": _now()}})
    from app.shared import entitlements

    entitlements.forget(workspace_id)
    person = await users.find_one({"id": user_id}, {"name": 1, "email": 1}) or {}
    await _record(workspace_id, staff, kind="action",
                  text=f"Changed {person.get('name') or person.get('email') or 'a member'} from {member.get('role')} to {body.role}. {body.reason}")
    return {"ok": True}


@router.get("/{workspace_id}/content")
@limiter.limit("60/minute")
async def content(
    request: Request, workspace_id: str,
    show: Literal["all", "flagged", "failed", "unapproved"] = "all",
    member: Optional[str] = Query(None, max_length=80), brand: Optional[str] = Query(None, max_length=80),
    platform: Optional[str] = Query(None, max_length=40), page: int = Query(0, ge=0, le=500),
) -> dict:
    """Every post, newest first, with what marks its quality: checks that failed, safety catches, review state and publishing.
    Read only: Ops never edits a customer's content."""
    await _workspace(workspace_id)
    db = get_db()
    query: dict[str, Any] = {"workspace_id": workspace_id, "deleted": {"$ne": True}}
    if show == "flagged":
        query["$or"] = [{"flagged_for_review": True}, {"quality_passed": False}]
    elif show == "failed":
        query["publish_status"] = "failed"
    elif show == "unapproved":
        query["approval_status"] = {"$in": ["rejected", "changes_requested"]}
    if member:
        query["user_id"] = member
    if brand:
        query["brand_id"] = brand
    if platform:
        query["platform"] = platform
    rows = await db["content_pieces"].find(query, {
        "_id": 0, "piece_id": 1, "platform": 1, "content": 1, "user_id": 1, "brand_id": 1, "quality_passed": 1, "quality_issues": 1,
        "flagged_for_review": 1, "readability_score": 1, "approval_status": 1, "publish_status": 1, "created_at": 1, "updated_at": 1,
        "version_count": 1, "media": 1,
    }).sort("created_at", -1).skip(page * PAGE).limit(PAGE).to_list(PAGE)
    ids = [r["piece_id"] for r in rows]
    safety: dict[str, list[str]] = {}
    async for e in db["safety_events"].find({"workspace_id": workspace_id, "piece_id": {"$in": ids}}, {"piece_id": 1, "outcome": 1, "categories": 1}):
        safety.setdefault(e.get("piece_id"), []).append(str(e.get("outcome") or ""))
    names = {u["id"]: u.get("name") or u.get("email") async for u in users.find({"id": {"$in": list({r.get("user_id") for r in rows})}}, {"id": 1, "name": 1, "email": 1})}
    brand_names = {b["id"]: _brand_name(b) async for b in db["brand_profiles"].find({"id": {"$in": list({r.get("brand_id") for r in rows})}}, {"id": 1, "identity": 1, "name": 1, "brand_type": 1})}
    return {
        "posts": [{
            "id": r["piece_id"], "platform": r.get("platform"), "excerpt": str(r.get("content") or "")[:240],
            "member": names.get(r.get("user_id")), "brand": brand_names.get(r.get("brand_id")),
            "quality_passed": r.get("quality_passed", True), "quality_issues": (r.get("quality_issues") or [])[:5],
            "flagged": bool(r.get("flagged_for_review")), "readability": r.get("readability_score"),
            "approval": str(getattr(r.get("approval_status"), "value", r.get("approval_status")) or ""),
            "publish": str(getattr(r.get("publish_status"), "value", r.get("publish_status")) or ""),
            "safety": safety.get(r["piece_id"], []), "versions": r.get("version_count") or 1, "has_media": bool(r.get("media")),
            "created_at": r.get("created_at"),
        } for r in rows],
        "page": page, "more": len(rows) == PAGE,
    }


@router.get("/{workspace_id}/billing")
@limiter.limit("60/minute")
async def billing(request: Request, workspace_id: str) -> dict:
    """Plan, renewal, payments with their receipts and refunds, credit packs bought and left, and this month's credits."""
    from app.billing import credits as CR
    from app.billing import lifecycle, topups
    from app.billing import subscriptions as S

    ws = await _workspace(workspace_id)
    sub = await S.current_for_workspace(workspace_id)
    renews = lifecycle.renewal_at(sub) if sub else None
    payments = [p async for p in S.payments_col().find({"workspace_id": workspace_id}, {"_id": 0}).sort("at", -1).limit(50)]
    receipts = {i["payment_id"]: i["number"] async for i in get_db()["billing_invoices"].find({"workspace_id": workspace_id}, {"payment_id": 1, "number": 1})}
    packs = [t async for t in topups.topups_col().find({"workspace_id": workspace_id}, {"_id": 0, "id": 1, "credits": 1, "amount": 1, "status": 1, "created_at": 1, "paid_at": 1}).sort("created_at", -1).limit(50)]
    pool = await CR.pool_for(workspace_id)
    left, until = await CR.bought_left(pool.id)
    summary = await CR.summary(workspace_id)
    return {
        "plan": ws.get("plan") or "free", "billing_status": ws.get("billing_status") or "none",
        "subscription": S.public(sub), "renews_at": renews.isoformat() if renews else None,
        "payments": [{**{k: p.get(k) for k in ("id", "amount", "currency", "status", "method", "at", "refund_status", "refunded", "topup_id")},
                      "receipt": receipts.get(p.get("id"))} for p in payments],
        "packs": packs, "bought_left": left, "bought_until": until.isoformat() if until else None,
        "credits": {k: summary.get(k) for k in ("period", "resets_on", "allowance", "granted", "carried", "used", "left", "limit", "bought_used")},
    }


@router.get("/{workspace_id}/export/{what}.csv")
@limiter.limit("10/minute")
async def export_one(
    request: Request, workspace_id: str, what: Literal["members", "content", "payments", "notes"],
    staff: dict = Depends(require_action("workspaces", "approve")),
) -> Response:
    """One workspace's members, posts, payments or Ops notes as a spreadsheet file. Needs Approve."""
    ws = await _workspace(workspace_id)
    name = re.sub(r"[^a-z0-9]+", "-", str(ws.get("name") or "workspace").lower()).strip("-") or "workspace"
    db = get_db()
    if what == "members":
        rows = (await members(request, workspace_id))["members"]
        data = [[m["name"], m["email"], m["role"], m["status"], m["joined_at"], m["last_active"], m["posts"]] for m in rows]
        head = ["Name", "Email", "Role", "Status", "Joined", "Last active", "Posts"]
    elif what == "content":
        cursor = db["content_pieces"].find({"workspace_id": workspace_id, "deleted": {"$ne": True}}, {
            "_id": 0, "piece_id": 1, "platform": 1, "content": 1, "user_id": 1, "quality_passed": 1, "quality_issues": 1,
            "flagged_for_review": 1, "approval_status": 1, "publish_status": 1, "created_at": 1,
        }).sort("created_at", -1).limit(EXPORT_MAX)
        pieces = await cursor.to_list(EXPORT_MAX)
        names = {u["id"]: u.get("name") or u.get("email") async for u in users.find({"id": {"$in": list({p.get("user_id") for p in pieces})}}, {"id": 1, "name": 1, "email": 1})}
        data = [[p["piece_id"], p.get("platform"), names.get(p.get("user_id"), ""), str(p.get("content") or "")[:500],
                 "yes" if p.get("quality_passed", True) else "no", "; ".join(p.get("quality_issues") or []), "yes" if p.get("flagged_for_review") else "",
                 str(getattr(p.get("approval_status"), "value", p.get("approval_status")) or ""),
                 str(getattr(p.get("publish_status"), "value", p.get("publish_status")) or ""), p.get("created_at")] for p in pieces]
        head = ["Post", "Platform", "Made by", "Text (start)", "Checks passed", "Check notes", "Flagged", "Review", "Publishing", "Made"]
    elif what == "payments":
        from app.billing import subscriptions as S

        pays = [p async for p in S.payments_col().find({"workspace_id": workspace_id}, {"_id": 0}).sort("at", -1).limit(EXPORT_MAX)]
        receipts = {i["payment_id"]: i["number"] async for i in db["billing_invoices"].find({"workspace_id": workspace_id}, {"payment_id": 1, "number": 1})}
        data = [[p.get("at"), p.get("id"), receipts.get(p.get("id"), ""), (p.get("amount") or 0) / 100, p.get("currency"), p.get("status"),
                 p.get("method") or "", p.get("refund_status") or "", (p.get("refunded") or 0) / 100, "credit pack" if p.get("topup_id") else "plan"]
                for p in pays]
        head = ["Date", "Payment", "Receipt", "Amount", "Currency", "Status", "Method", "Refund", "Refunded", "For"]
    else:
        rows = await notes_col().find({"workspace_id": workspace_id}, {"_id": 0}).sort("at", -1).limit(EXPORT_MAX).to_list(EXPORT_MAX)
        data = [[n.get("at"), n.get("by_name"), n.get("kind"), "yes" if n.get("flag") else "", "open" if n.get("open") else "",
                 n.get("text"), n.get("resolved_by") or "", n.get("resolution") or ""] for n in rows]
        head = ["When", "By", "Kind", "Flag", "Open", "Text", "Resolved by", "Resolution"]
    await _log_export(staff, f"{what} of {ws.get('name')}", len(data), workspace_id=workspace_id)
    return csv_response(f"ekavo-{name}-{what}", head, data)


@router.get("/{workspace_id}/activity")
@limiter.limit("60/minute")
async def activity(request: Request, workspace_id: str) -> dict:
    await _workspace(workspace_id)
    rows = await get_db()["activity_entries"].find({"workspace_id": workspace_id}, {
        "_id": 1, "title": 1, "description": 1, "status": 1, "category": 1, "actor": 1, "occurred_at": 1,
    }).sort("occurred_at", -1).limit(50).to_list(50)
    return {"activity": [{
        "id": r["_id"], "title": r.get("title"), "description": r.get("description"), "status": r.get("status"),
        "category": r.get("category"), "by": (r.get("actor") or {}).get("name"), "at": r.get("occurred_at"),
    } for r in rows]}


# ── Ops notes, flags and history ─────────────────────────────────────────

async def _record(workspace_id: str, staff: dict, *, kind: Literal["note", "action"], text: str, flag: bool = False) -> dict:
    doc = {
        "id": uuid4().hex, "workspace_id": workspace_id, "kind": kind, "text": text[:1000], "flag": flag, "open": flag,
        "by": staff.get("id"), "by_name": staff.get("name") or staff.get("email"), "at": _now(),
    }
    await notes_col().insert_one(dict(doc))
    return doc


@router.get("/{workspace_id}/notes")
@limiter.limit("60/minute")
async def notes(request: Request, workspace_id: str) -> dict:
    await _workspace(workspace_id)
    rows = await notes_col().find({"workspace_id": workspace_id}, {"_id": 0}).sort("at", -1).limit(200).to_list(200)
    return {"notes": rows}


class NoteIn(BaseModel):
    text: str = Field(..., min_length=3, max_length=1000)
    flag: bool = False


@router.post("/{workspace_id}/notes")
@limiter.limit("30/minute")
async def add_note(request: Request, workspace_id: str, body: NoteIn, staff: dict = Depends(get_current_user)) -> dict:
    """A note for the team, or a flag to follow up (open until someone resolves it)."""
    await _workspace(workspace_id)
    doc = await _record(workspace_id, staff, kind="note", text=body.text.strip(), flag=body.flag)
    return {"note": {k: v for k, v in doc.items() if k != "_id"}}


class ResolveIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/{workspace_id}/notes/{note_id}/resolve")
@limiter.limit("30/minute")
async def resolve_flag(request: Request, workspace_id: str, note_id: str, body: ResolveIn, staff: dict = Depends(get_current_user)) -> dict:
    res = await notes_col().update_one(
        {"id": note_id, "workspace_id": workspace_id, "flag": True, "open": True},
        {"$set": {"open": False, "resolved_by": staff.get("name") or staff.get("email"), "resolved_at": _now(), "resolution": body.reason}},
    )
    if not res.modified_count:
        raise HTTPException(status_code=404, detail="That flag is already resolved or doesn't exist.")
    return {"ok": True}


async def _log_export(staff: dict, what: str, rows: int, workspace_id: Optional[str] = None) -> None:
    """Every export is kept in the Team history (and the workspace's own Ops history when it is about one workspace)."""
    from app.core import staff as ST

    await ST.audit(staff, "export", workspace_id or "workspaces", after={"what": what, "rows": rows})
    if workspace_id:
        await _record(workspace_id, staff, kind="action", text=f"Exported {what} ({rows} rows).")


# ── the person view ─────────────────────────────────────────────────────

class PeopleFilters:
    def __init__(self, q: Optional[str] = Query(None, max_length=100), staff_only: bool = False) -> None:
        self.q, self.staff_only = q, staff_only


def _people_query(f: PeopleFilters) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if f.q and f.q.strip():
        term = re.escape(f.q.strip())
        query["$or"] = [{"id": f.q.strip()}, {"email": {"$regex": term, "$options": "i"}}, {"name": {"$regex": term, "$options": "i"}},
                        {"username": {"$regex": term, "$options": "i"}}]
    if f.staff_only:
        query["$and"] = [{"$or": [{"is_platform_staff": True}, {"is_master_admin": True}]}]
    return query


async def _people_rows(query: dict[str, Any], *, skip: int, limit: int) -> list[dict]:
    from app.core import staff as ST

    rows = await users.find(query, {"_id": 0, "id": 1, "name": 1, "email": 1, "created_at": 1, "last_active": 1, "is_master_admin": 1,
                                    "is_platform_staff": 1, "staff": 1}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    counts: dict[str, int] = {}
    async for m in workspace_members.aggregate([
        {"$match": {"user_id": {"$in": [r["id"] for r in rows]}, "status": "active"}}, {"$group": {"_id": "$user_id", "n": {"$sum": 1}}},
    ]):
        counts[m["_id"]] = int(m["n"])
    out = []
    for r in rows:
        if r.get("is_master_admin"):
            level = "master_admin"
        elif r.get("is_platform_staff"):
            level = (r.get("staff") or {}).get("level") or "admin"
        else:
            level = None
        out.append({"id": r["id"], "name": r.get("name") or "", "email": r.get("email") or "", "created_at": r.get("created_at"),
                    "last_active": r.get("last_active"), "workspaces": counts.get(r["id"], 0),
                    "staff": ST.LEVEL_LABEL.get(level) if level else None})
    return out


@people_router.get("")
@limiter.limit("60/minute")
async def list_people(request: Request, f: PeopleFilters = Depends(), page: int = Query(0, ge=0, le=500)) -> dict:
    rows = await _people_rows(_people_query(f), skip=page * PAGE, limit=PAGE)
    return {"people": rows, "page": page, "more": len(rows) == PAGE}


@people_router.get("/export.csv")
@limiter.limit("10/minute")
async def export_people(request: Request, f: PeopleFilters = Depends(), staff: dict = Depends(require_action("workspaces", "approve"))) -> Response:
    rows = await _people_rows(_people_query(f), skip=0, limit=EXPORT_MAX)
    await _log_export(staff, "people", len(rows))
    return csv_response("ekavo-people", ["Name", "Email", "Id", "Signed up", "Last active", "Workspaces", "Staff"],
                        [[r["name"], r["email"], r["id"], r["created_at"], r["last_active"], r["workspaces"], r["staff"] or ""] for r in rows])


@people_router.get("/{user_id}")
@limiter.limit("60/minute")
async def person(request: Request, user_id: str) -> dict:
    """Everything about one person: their account, every workspace they are in and their role there, what they made, payments for
    the workspaces they own, support tickets, devices with notifications on, staff access, and what they did lately."""
    from app.billing import subscriptions as S
    from app.core import staff as ST

    u = await users.find_one({"id": user_id[:80]}, {"_id": 0, "id": 1, "name": 1, "email": 1, "username": 1, "created_at": 1, "last_active": 1,
                                                    "language": 1, "default_workspace_id": 1, "is_master_admin": 1, "is_platform_staff": 1, "staff": 1})
    if not u:
        raise HTTPException(status_code=404, detail="That person doesn't exist.")
    db = get_db()
    memberships = await workspace_members.find({"user_id": user_id}, {"_id": 0}).to_list(500)
    ws_docs = {w["id"]: w async for w in workspaces.find(
        {"id": {"$in": [m["workspace_id"] for m in memberships]}},
        {"_id": 0, "id": 1, "name": 1, "plan": 1, "purpose": 1, "owner_id": 1, "deleted_at": 1, "billing_status": 1})}
    made: dict[str, int] = {}
    async for row in db["content_pieces"].aggregate([
        {"$match": {"user_id": user_id, "deleted": {"$ne": True}}}, {"$group": {"_id": "$workspace_id", "n": {"$sum": 1}}},
    ]):
        made[str(row["_id"])] = int(row["n"])
    owned = [w["id"] for w in ws_docs.values() if w.get("owner_id") == user_id]
    payments = [p async for p in S.payments_col().find(
        {"workspace_id": {"$in": owned}},
        {"_id": 0, "id": 1, "workspace_id": 1, "amount": 1, "currency": 1, "status": 1, "at": 1, "refund_status": 1, "topup_id": 1},
    ).sort("at", -1).limit(30)]
    tickets = await db["support_tickets"].find(
        {"created_by": user_id}, {"_id": 0, "id": 1, "number": 1, "subject": 1, "status": 1, "workspace_name": 1, "created_at": 1},
    ).sort("created_at", -1).limit(30).to_list(30)
    devices = await db["push_devices"].find({"user_id": user_id}, {"_id": 0, "label": 1, "created_at": 1, "last_ok_at": 1}).to_list(10)
    recent = await db["activity_entries"].find(
        {"actor.user_id": user_id}, {"_id": 1, "title": 1, "workspace_id": 1, "occurred_at": 1, "status": 1},
    ).sort("occurred_at", -1).limit(30).to_list(30)
    access = await ST.access_for(u)

    def ws(wid: str) -> dict:
        return ws_docs.get(wid) or {}

    return {
        "id": u["id"], "name": u.get("name") or "", "email": u.get("email") or "", "username": u.get("username"), "language": u.get("language"),
        "created_at": u.get("created_at"), "last_active": u.get("last_active"),
        "staff": {"level": access.get("level"), "level_label": ST.LEVEL_LABEL.get(access.get("level") or "", ""), "role": access.get("role_name"),
                  "designation": (u.get("staff") or {}).get("designation")} if access.get("level") else None,
        "workspaces": [{
            "id": m["workspace_id"], "name": ws(m["workspace_id"]).get("name") or "A removed workspace", "role": m.get("role"),
            "status": m.get("status"), "joined_at": m.get("joined_at"), "plan": ws(m["workspace_id"]).get("plan") or "free",
            "purpose": ws(m["workspace_id"]).get("purpose"), "billing_status": ws(m["workspace_id"]).get("billing_status"),
            "posts_made": made.get(m["workspace_id"], 0), "is_default": m["workspace_id"] == u.get("default_workspace_id"),
            "deleted": bool(ws(m["workspace_id"]).get("deleted_at")),
        } for m in memberships],
        "posts_made": sum(made.values()),
        "payments": [{**p, "workspace": ws(p.get("workspace_id")).get("name")} for p in payments],
        "tickets": tickets, "devices": devices,
        "recent": [{"id": r["_id"], "title": r.get("title"), "workspace": ws(r.get("workspace_id")).get("name"), "workspace_id": r.get("workspace_id"),
                    "at": r.get("occurred_at"), "status": r.get("status")} for r in recent],
    }


async def create_indexes() -> None:
    await notes_col().create_index([("workspace_id", 1), ("at", -1)])
    await notes_col().create_index([("flag", 1), ("open", 1)])
