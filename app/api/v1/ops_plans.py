"""Ops: the plans table and per-workspace plan overrides.

Reading needs platform staff; changing anything is for the Ops owner (a master admin), because a plan applies to every workspace on
it. Every change is written to `billing_audit` with who, what, the value before and after, and the reason given.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.billing import plans as P
from app.core.auth import require_platform_staff
from app.core.config import settings
from app.core.middleware import limiter
from app.db.mongo import get_db, users, workspaces
from app.shared import entitlements

router = APIRouter()
logger = logging.getLogger(__name__)


async def _ops_owner(user: dict = Depends(require_platform_staff)) -> dict:
    if not user.get("is_master_admin"):
        raise HTTPException(status_code=403, detail="Only the Ops owner can change plans.")
    return user


def _audit_col():
    return get_db()["billing_audit"]


async def _audit(user: dict, action: str, target: str, before: Any, after: Any, reason: str, target_name: str = "") -> None:
    await _audit_col().insert_one({
        "at": datetime.now(timezone.utc), "by": user["id"], "by_email": user.get("email"), "action": action, "target": target,
        "target_name": target_name or target,
        "before": before, "after": after, "reason": reason,
    })


def _plan_row(pid: str, plan: dict[str, Any]) -> dict[str, Any]:
    default = P.DEFAULT_PLANS[pid]
    return {
        "id": pid, "name": plan.get("name"), "per_seat": bool(plan.get("per_seat")), "min_seats": plan.get("min_seats"),
        "max_seats": plan.get("max_seats"), "limits": plan["limits"], "defaults": default["limits"],
        "changed": sorted(k for k in P.LIMITS if plan["limits"].get(k) != default["limits"].get(k)),
        "example": P.limits_for(plan, plan.get("min_seats")),
    }


@router.get("")
@limiter.limit("60/minute")
async def list_plans(request: Request, _: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    table = await entitlements.load_plans()
    return {
        "enforced": bool(settings.BILLING_ENFORCED),
        "limits": [{"key": k, **meta} for k, meta in P.LIMITS.items()],
        "plans": [_plan_row(pid, table[pid]) for pid in P.PLAN_IDS],
    }


class PlanChange(BaseModel):
    limits: dict[str, Any] = Field(default_factory=dict)
    min_seats: Optional[int] = Field(None, ge=1, le=1000)
    max_seats: Optional[int] = Field(None, ge=1, le=1000)
    reason: str = Field(..., min_length=3, max_length=300)


@router.put("/{plan_id}")
@limiter.limit("30/minute")
async def change_plan(request: Request, plan_id: str, body: PlanChange, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    """Change some of a plan's limits. Only the limits sent change; send null for no limit."""
    if plan_id not in P.PLAN_IDS:
        raise HTTPException(status_code=404, detail="No such plan.")
    unknown = sorted(set(body.limits) - set(P.LIMITS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Not a limit: {', '.join(unknown)}.")
    clean: dict[str, Any] = {}
    for key, rule in body.limits.items():
        try:
            clean[key] = P.validate_rule(rule)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{P.LIMITS[key]['label']}: {exc}") from exc
    table = await entitlements.load_plans()
    current = table[plan_id]
    low = body.min_seats if body.min_seats is not None else current.get("min_seats")
    high = body.max_seats if body.max_seats is not None else current.get("max_seats")
    if low > high:
        raise HTTPException(status_code=400, detail="The fewest seats cannot be more than the most seats.")
    before = {k: current["limits"].get(k) for k in clean}
    update: dict[str, Any] = {f"limits.{k}": v for k, v in clean.items()}
    if body.min_seats is not None:
        update["min_seats"] = body.min_seats
    if body.max_seats is not None:
        update["max_seats"] = body.max_seats
    update["updated_at"] = datetime.now(timezone.utc)
    update["updated_by"] = user["id"]
    await get_db()["billing_plans"].update_one({"id": plan_id}, {"$set": update}, upsert=True)
    entitlements.forget_plans()
    await _audit(user, "plan.change", plan_id, before, {**clean, "min_seats": body.min_seats, "max_seats": body.max_seats}, body.reason,
                 str(current.get("name") or plan_id))
    table = await entitlements.load_plans()
    return _plan_row(plan_id, table[plan_id])


class Reason(BaseModel):
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/{plan_id}/reset")
@limiter.limit("10/minute")
async def reset_plan(request: Request, plan_id: str, body: Reason, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    """Put a plan back to the values Ekavo ships with."""
    if plan_id not in P.PLAN_IDS:
        raise HTTPException(status_code=404, detail="No such plan.")
    saved = await get_db()["billing_plans"].find_one({"id": plan_id}, {"_id": 0})
    await get_db()["billing_plans"].delete_one({"id": plan_id})
    entitlements.forget_plans()
    await _audit(user, "plan.reset", plan_id, saved, None, body.reason, str(P.DEFAULT_PLANS[plan_id]["name"]))
    table = await entitlements.load_plans()
    return _plan_row(plan_id, table[plan_id])


# ── workspaces ───────────────────────────────────────────────────────────

_SAFE = re.compile(r"[^\w@.\- ]")


@router.get("/workspaces")
@limiter.limit("60/minute")
async def find_workspaces(
    request: Request, q: str = Query("", max_length=100), _: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """Workspaces by id, name or owner email, with the plan each is on."""
    q = _SAFE.sub("", q.strip())
    if len(q) < 2:
        return {"workspaces": []}
    owner_ids = [u["id"] async for u in users.find({"email": {"$regex": re.escape(q), "$options": "i"}}, {"id": 1}).limit(20)]
    query = {"deleted_at": {"$exists": False}, "$or": [
        {"id": q}, {"name": {"$regex": re.escape(q), "$options": "i"}}, {"owner_id": {"$in": owner_ids}},
    ]}
    rows = await workspaces.find(query, {"_id": 0, "id": 1, "name": 1, "owner_id": 1, "plan": 1, "tier": 1, "billing_group_id": 1,
                                         "entitlement_overrides": 1, "is_personal": 1}).limit(25).to_list(25)
    emails = {u["id"]: u.get("email") async for u in users.find({"id": {"$in": [r["owner_id"] for r in rows]}}, {"id": 1, "email": 1})}
    out = []
    for r in rows:
        ent = await entitlements.entitlements_for(r["id"])
        out.append({
            "id": r["id"], "name": r.get("name"), "owner_email": emails.get(r["owner_id"]), "is_personal": bool(r.get("is_personal")),
            "plan": ent.plan, "plan_name": ent.plan_name, "team_size": r.get("tier"), "group_id": ent.group_id,
            "comp": ent.comp, "has_override": bool(r.get("entitlement_overrides")),
        })
    return {"workspaces": out}


@router.get("/workspaces/{workspace_id}")
@limiter.limit("60/minute")
async def workspace_plan(request: Request, workspace_id: str, _: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    ws = await workspaces.find_one({"id": workspace_id}, {"_id": 0, "id": 1, "name": 1, "entitlement_overrides": 1})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    entitlements.forget(workspace_id)
    summary = await entitlements.summary(await entitlements.entitlements_for(workspace_id), for_ops=True)
    override = dict(ws.get("entitlement_overrides") or {})
    if override.get("expires_at"):
        override["expires_at"] = override["expires_at"].isoformat()
    if override.get("set_at"):
        override["set_at"] = override["set_at"].isoformat()
    return {"name": ws.get("name"), "override": override or None, **summary}


class OverrideBody(BaseModel):
    plan: Optional[str] = None
    seats: Optional[int] = Field(None, ge=1, le=1000)
    limits: dict[str, Any] = Field(default_factory=dict)
    expires_at: Optional[datetime] = None
    note: str = Field(..., min_length=3, max_length=300)


@router.put("/workspaces/{workspace_id}/override")
@limiter.limit("30/minute")
async def set_override(request: Request, workspace_id: str, body: OverrideBody, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    """Give one workspace a complimentary plan, a seat count, or different limits, with a note and an optional end date."""
    ws = await workspaces.find_one({"id": workspace_id}, {"id": 1, "name": 1, "entitlement_overrides": 1})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if body.plan is not None and body.plan not in P.PLAN_IDS:
        raise HTTPException(status_code=400, detail="No such plan.")
    unknown = sorted(set(body.limits) - set(P.LIMITS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Not a limit: {', '.join(unknown)}.")
    limits: dict[str, Any] = {}
    for key, rule in body.limits.items():
        try:
            limits[key] = P.validate_rule(rule)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{P.LIMITS[key]['label']}: {exc}") from exc
    expires = body.expires_at
    if expires is not None:
        expires = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="The end date must be in the future.")
    if body.plan is None and body.seats is None and not limits:
        raise HTTPException(status_code=400, detail="Choose a plan, a seat count or at least one limit.")
    override = {
        **({"plan": body.plan} if body.plan else {}), **({"seats": body.seats} if body.seats else {}), "limits": limits,
        "note": body.note.strip(), "expires_at": expires, "set_by": user["id"], "set_at": datetime.now(timezone.utc),
    }
    await workspaces.update_one({"id": workspace_id}, {"$set": {"entitlement_overrides": override}})
    entitlements.forget(workspace_id)
    await _audit(user, "workspace.override", workspace_id, ws.get("entitlement_overrides"), {**override, "set_at": None}, body.note,
                 str(ws.get("name") or workspace_id))
    return await workspace_plan(request, workspace_id, user)


@router.delete("/workspaces/{workspace_id}/override")
@limiter.limit("30/minute")
async def clear_override(request: Request, workspace_id: str, body: Reason, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    ws = await workspaces.find_one({"id": workspace_id}, {"id": 1, "name": 1, "entitlement_overrides": 1})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    await workspaces.update_one({"id": workspace_id}, {"$unset": {"entitlement_overrides": ""}})
    entitlements.forget(workspace_id)
    await _audit(user, "workspace.override_cleared", workspace_id, ws.get("entitlement_overrides"), None, body.reason,
                 str(ws.get("name") or workspace_id))
    return await workspace_plan(request, workspace_id, user)


@router.get("/audit")
@limiter.limit("60/minute")
async def audit_log(request: Request, limit: int = Query(50, ge=1, le=200), _: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    rows = await _audit_col().find({}, {"_id": 0}).sort("at", -1).limit(limit).to_list(limit)
    for r in rows:
        r["at"] = r["at"].isoformat()
    return {"changes": rows}


# ── credits ──────────────────────────────────────────────────────────────

@router.get("/credits/costs")
@limiter.limit("60/minute")
async def credit_costs(request: Request, _: dict = Depends(require_platform_staff)) -> dict[str, Any]:
    """The costs table: what each action costs, its default, and whether it is charged or system work."""
    from app.billing import costs as C

    table = await C.costs()
    return {"modules": [{"key": k, "label": v} for k, v in C.MODULES.items()], "costs": [C.public_row(k, v) for k, v in table.items()]}


class CostChange(BaseModel):
    credits: int = Field(..., ge=0, le=1000)
    reason: str = Field(..., min_length=3, max_length=300)


@router.put("/credits/costs/{action}")
@limiter.limit("30/minute")
async def change_credit_cost(request: Request, action: str, body: CostChange, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    """Change what one action costs. New charges use it within a minute; what was charged before stays as it was."""
    from app.billing import costs as C

    if action not in C.DEFAULT_COSTS:
        raise HTTPException(status_code=404, detail="No such action.")
    if C.DEFAULT_COSTS[action]["kind"] == "passive" and body.credits:
        raise HTTPException(status_code=400, detail="System work is always free.")
    before = (await C.costs())[action]["credits"]
    if body.credits == C.DEFAULT_COSTS[action]["credits"]:
        await get_db()["billing_credit_costs"].delete_one({"id": action})
    else:
        await get_db()["billing_credit_costs"].update_one(
            {"id": action}, {"$set": {"id": action, "credits": body.credits, "updated_at": datetime.now(timezone.utc), "by": user["id"]}},
            upsert=True,
        )
    C.forget()
    await _audit(user, "credits.cost", action, before, body.credits, body.reason, str(C.DEFAULT_COSTS[action]["label"]))
    table = await C.costs()
    return C.public_row(action, table[action])


@router.get("/credits/workspaces/{workspace_id}")
@limiter.limit("60/minute")
async def workspace_credits(
    request: Request, workspace_id: str, period: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
    before: Optional[str] = Query(None, max_length=40), _: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """One workspace's credits this month and its ledger, newest first."""
    from app.billing import credits

    if not await workspaces.find_one({"id": workspace_id}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return {"balance": await credits.summary(workspace_id), "ledger": await credits.ledger(workspace_id, period=period, before=before, for_ops=True)}


class CreditGrant(BaseModel):
    credits: int = Field(..., ge=1, le=1_000_000)
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/credits/workspaces/{workspace_id}/grant")
@limiter.limit("30/minute")
async def grant_credits(request: Request, workspace_id: str, body: CreditGrant, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    """Add credits to this month. They are shared by every workspace on the same plan, and end with the month."""
    from app.billing import credits

    ws = await workspaces.find_one({"id": workspace_id}, {"id": 1, "name": 1})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    balance = await credits.grant(workspace_id, body.credits, by=user["id"], note=body.reason.strip())
    await _audit(user, "credits.grant", workspace_id, None, body.credits, body.reason, str(ws.get("name") or workspace_id))
    return {"balance": balance}


@router.post("/credits/workspaces/{workspace_id}/refund")
@limiter.limit("30/minute")
async def refund_credits(request: Request, workspace_id: str, body: CreditGrant, user: dict = Depends(_ops_owner)) -> dict[str, Any]:
    """Give back credits charged this month, for work that went wrong on Ekavo's side."""
    from app.billing import credits

    ws = await workspaces.find_one({"id": workspace_id}, {"id": 1, "name": 1})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    balance = await credits.refund(workspace_id, body.credits, by=user["id"], note=body.reason.strip())
    await _audit(user, "credits.refund", workspace_id, None, body.credits, body.reason, str(ws.get("name") or workspace_id))
    return {"balance": balance}


@router.get("/credits/report")
@limiter.limit("30/minute")
async def credit_report(
    request: Request, period: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"), _: dict = Depends(require_platform_staff),
) -> dict[str, Any]:
    """The month's AI and render cost at paid rates, against the credits charged."""
    from app.billing import report

    return await report.monthly(period)
