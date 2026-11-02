"""Ops: payments. The mode in use (test or live), billing settings (the seller details on invoices, GST registration, whether checkout
is open), subscriptions, and the payments watch.

Reading needs platform staff; changing settings is for the Ops owner (a master admin). Every change is written to `billing_audit`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.billing import invoices
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.billing import watch
from app.billing.provider import ProviderError, get_provider
from app.core.auth import require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import get_db

router = APIRouter()


async def _ops_owner(user: dict = Depends(require_platform_staff)) -> dict:
    if not user.get("is_master_admin"):
        raise HTTPException(status_code=403, detail="Only the Ops owner can change billing settings.")
    return user


@router.get("/overview")
@limiter.limit("60/minute")
async def overview(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    provider = get_provider()
    cfg = await invoices.load_settings()
    counts: dict[str, int] = {}
    async for row in S.subs_col().aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        counts[str(row["_id"])] = int(row["n"])
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    year_ago = now - timedelta(days=365)
    paying = len(await S.subs_col().distinct("workspace_id", {"status": {"$in": list(S.PAID_STATES)}, "mode": provider.mode}))

    async def _sum(flt: dict) -> int:
        rows = await S.payments_col().aggregate([{"$match": flt}, {"$group": {"_id": None, "n": {"$sum": "$amount"}}}]).to_list(1)
        return int(rows[0]["n"]) if rows else 0

    paid_flt = {"status": "captured", "currency": "INR", "mode": provider.mode, "refund_status": {"$ne": "refunded"}}
    last_12 = await _sum({**paid_flt, "at": {"$gte": year_ago}})
    return {
        "mode": provider.mode,
        "configured": provider.configured(),
        "webhook_secret_set": bool(getattr(provider, "_webhook_secret", "")),
        "settings": cfg,
        "seller_ready": invoices.seller_ready(cfg),
        "subscriptions_by_status": counts,
        "founding_taken": await S.founding_taken(),
        "founding_left": await S.founding_left(),
        "founding_limit": PR.founding_limit(),
        "paying_workspaces": paying,
        "money_this_month": await _sum({**paid_flt, "at": {"$gte": month_start}}),
        "failed_this_month": await S.payments_col().count_documents({"status": "failed", "mode": provider.mode, "at": {"$gte": month_start}}),
        # GST: registration is needed past Rs 20 lakh a year; Ops is warned from Rs 15 lakh so it happens in time
        "paid_last_12_months": last_12, "gst_warn_at": GST_WARN_AT, "gst_register_at": GST_REGISTER_AT,
        "gst_warning": not cfg.get("gst_registered") and last_12 >= GST_WARN_AT,
        "open_findings": len(await watch.open_findings()),
    }


GST_WARN_AT = 15 * 100_000 * 100   # Rs 15 lakh, in paise
GST_REGISTER_AT = 20 * 100_000 * 100   # Rs 20 lakh, in paise


async def _names(ids: list[str]) -> dict[str, str]:
    from app.db.mongo import workspaces

    return {w["id"]: w.get("name") or "" async for w in workspaces.find({"id": {"$in": ids}}, {"id": 1, "name": 1})}


@router.get("/failed")
@limiter.limit("60/minute")
async def failed(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    """Renewals being retried, workspaces limited after their grace, and renewals waiting for the bank's approval."""
    from app.billing import lifecycle

    now = datetime.now(timezone.utc)
    subs = [s async for s in S.subs_col().find({"status": {"$in": ["pending", "halted"]}}, {"_id": 0}).sort("updated_at", -1).limit(200)]
    names = await _names([s["workspace_id"] for s in subs])
    rows = []
    for s in subs:
        grace = S._aware(s.get("grace_until"))
        approval = await lifecycle.approvals_col().find_one({"subscription_id": s["id"], "status": "open"}, {"_id": 1})
        status = "needs_approval" if approval else S.billing_status_of(s, now)
        rows.append({
            "workspace_id": s["workspace_id"], "workspace": names.get(s["workspace_id"], ""), "amount": s.get("amount"), "currency": s.get("currency"),
            "since": s.get("updated_at"), "grace_left_days": max(0, (grace - now).days) if grace else None,
            "status": {"past_due": "retrying", "limited": "limited", "needs_approval": "needs_approval"}.get(status, status),
        })
    return {"failed": rows}


@router.get("/workspace/{workspace_id}")
@limiter.limit("60/minute")
async def workspace_billing(request: Request, workspace_id: str, user: dict = Depends(require_platform_staff)) -> dict:
    from app.billing import lifecycle

    sub = await S.current_for_workspace(workspace_id[:80])
    if not sub:
        raise HTTPException(status_code=404, detail="This workspace has never had a paid plan.")
    renews = lifecycle.renewal_at(sub)
    payments = [p async for p in S.payments_col().find({"workspace_id": sub["workspace_id"]}, {"_id": 0}).sort("at", -1).limit(50)]
    return {
        "workspace": (await _names([sub["workspace_id"]])).get(sub["workspace_id"], ""), "subscription": sub,
        "renews_at": renews.isoformat() if renews else None,
        "receipts": await invoices._invoices_col().count_documents({"workspace_id": sub["workspace_id"]}), "payments": payments,
    }


class RefundIn(BaseModel):
    payment_id: str = Field(..., min_length=1, max_length=80)
    amount: Optional[int] = Field(default=None, ge=100, description="In paise; empty refunds the whole payment")
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/workspace/{workspace_id}/refund")
@limiter.limit("10/minute")
async def refund(request: Request, workspace_id: str, body: RefundIn, user: dict = Depends(_ops_owner)) -> dict:
    """Give money back through Razorpay (it lands in a few days). The receipt shows it once Razorpay confirms."""
    pay = await S.payments_col().find_one({"id": body.payment_id, "workspace_id": workspace_id[:80], "status": "captured"}, {"_id": 0})
    if not pay:
        raise HTTPException(status_code=404, detail="We couldn't find that payment for this workspace.")
    left = int(pay.get("amount") or 0) - S.refunded_total(pay)
    if left <= 0:
        raise HTTPException(status_code=400, detail="This payment was already refunded in full.")
    if body.amount is not None and body.amount > left:
        raise HTTPException(status_code=400, detail=f"That is more than is left to refund ({PR.format_amount(left, pay.get('currency') or 'INR')}).")
    try:
        result = await get_provider().refund(body.payment_id, amount=body.amount, notes={"reason": body.reason[:200], "by": user["id"]})
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    await _audit(user, "refund", workspace_id, before={"payment": body.payment_id, "amount": pay.get("amount")},
                 after={"refunded": body.amount or left, "refund_id": result.get("id")}, reason=body.reason)
    return {"refund": {"id": result.get("id"), "status": result.get("status"), "amount": result.get("amount")}}


class ExtendIn(BaseModel):
    days: int = Field(..., ge=1, le=30)
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/workspace/{workspace_id}/extend")
@limiter.limit("10/minute")
async def extend(request: Request, workspace_id: str, body: ExtendIn, user: dict = Depends(_ops_owner)) -> dict:
    """More days of grace after a failed payment, for a bank outage or a customer who asked; everything keeps working meanwhile."""
    sub = await S.subs_col().find_one({"workspace_id": workspace_id[:80], "status": {"$in": ["pending", "halted"]}}, {"_id": 0})
    if not sub:
        raise HTTPException(status_code=400, detail="Only a failed payment's grace can be extended, and this workspace has none.")
    now = datetime.now(timezone.utc)
    start = max(S._aware(sub.get("grace_until")) or now, now)
    until = start + timedelta(days=body.days)
    await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"grace_until": until}})
    await S.apply_for_workspace(sub["workspace_id"])
    await _audit(user, "extend_grace", workspace_id, before={"grace_until": sub.get("grace_until")}, after={"grace_until": until, "days": body.days}, reason=body.reason)
    return {"grace_until": until}


@router.get("/history")
@limiter.limit("60/minute")
async def history(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    """Every change Ops made to billing, with who, what and why."""
    actions = ["refund", "extend_grace", "billing_settings", "prices_changed", "credits.grant", "credits.refund", "workspace.override", "workspace.override_cleared"]
    rows = [r async for r in get_db()["billing_audit"].find({"action": {"$in": actions}}, {"_id": 0}).sort("at", -1).limit(100)]
    return {"history": rows}


async def _audit(user: dict, action: str, target: str, *, before: Any, after: Any, reason: str) -> None:
    names = await _names([target])
    await get_db()["billing_audit"].insert_one({
        "at": datetime.now(timezone.utc), "by": user["id"], "by_email": user.get("email"), "action": action, "target": target,
        "target_name": names.get(target, target), "before": before, "after": after, "reason": reason,
    })


class SellerIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    address: Optional[str] = Field(default=None, max_length=500)
    state: Optional[str] = Field(default=None, max_length=100)
    email: Optional[str] = Field(default=None, max_length=200)
    gstin: Optional[str] = Field(default=None, max_length=15)


class SettingsIn(BaseModel):
    seller: Optional[SellerIn] = None
    gst_registered: Optional[bool] = None
    checkout_open: Optional[bool] = None
    invoice_prefix: Optional[str] = Field(default=None, min_length=2, max_length=8, pattern=r"^[A-Z0-9]+$")
    reason: str = Field(..., min_length=3, max_length=300)


@router.put("/settings")
@limiter.limit("20/minute")
async def update_settings(request: Request, body: SettingsIn, user: dict = Depends(_ops_owner)) -> dict:
    before = await invoices.load_settings()
    changes = body.model_dump(exclude_none=True, exclude={"reason"})
    if "seller" in changes:
        changes["seller"] = {k: v.strip() for k, v in changes["seller"].items() if isinstance(v, str)}
    after = await invoices.save_settings(changes)
    if after.get("gst_registered") and not str(after["seller"].get("gstin") or "").strip():
        await invoices.save_settings({"gst_registered": False})
        raise HTTPException(status_code=400, detail="Add the GSTIN before switching GST invoices on.")
    await get_db()["billing_audit"].insert_one({
        "at": datetime.now(timezone.utc), "by": user["id"], "by_email": user.get("email"), "action": "billing_settings",
        "target": "billing", "target_name": "Billing settings", "before": before, "after": after, "reason": body.reason,
    })
    return {"settings": after, "seller_ready": invoices.seller_ready(after)}


@router.get("/prices")
@limiter.limit("60/minute")
async def get_prices(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    """The price book (plans, founding, the lower Large seat price, yearly months, credit packs) and the values it started from."""
    return {"book": await PR.load_book(force=True), "defaults": PR.DEFAULT_BOOK}


class PricesIn(BaseModel):
    book: dict[str, Any]
    reason: str = Field(..., min_length=3, max_length=300)


@router.put("/prices")
@limiter.limit("20/minute")
async def put_prices(request: Request, body: PricesIn, user: dict = Depends(_ops_owner)) -> dict:
    """Save new prices. They apply to new checkouts, changes and packs; running subscriptions keep the price they were bought at."""
    try:
        book = await PR.save_book(body.book, by=user.get("email") or user["id"], reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"book": book}


@router.get("/subscriptions")
@limiter.limit("60/minute")
async def list_subscriptions(
    request: Request, status: Optional[str] = Query(default=None, max_length=20), workspace_id: Optional[str] = Query(default=None, max_length=80),
    user: dict = Depends(require_platform_staff),
) -> dict:
    flt: dict = {}
    if status:
        flt["status"] = status
    if workspace_id:
        flt["workspace_id"] = workspace_id
    rows = [r async for r in S.subs_col().find(flt, {"_id": 0}).sort("created_at", -1).limit(200)]
    return {"subscriptions": rows}


@router.get("/payments")
@limiter.limit("60/minute")
async def list_payments(request: Request, status: Optional[str] = Query(default=None, max_length=20), user: dict = Depends(require_platform_staff)) -> dict:
    flt = {"status": status} if status else {}
    rows = [r async for r in S.payments_col().find(flt, {"_id": 0}).sort("at", -1).limit(200)]
    names = await _names([r["workspace_id"] for r in rows if r.get("workspace_id")])
    for r in rows:
        r["workspace"] = names.get(r.get("workspace_id") or "", "")
    return {"payments": rows}


@router.get("/watch")
@limiter.limit("60/minute")
async def watch_findings(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    return {"findings": await watch.open_findings()}


@router.post("/watch/run")
@limiter.limit("6/minute")
async def run_watch(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    """Run the payments watch now instead of waiting for its quarter-hour check."""
    await watch.run_checks()
    return {"findings": await watch.open_findings()}
