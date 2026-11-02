"""Paid plans for members: prices for the plan picker, checkout, the workspace's subscription and invoices, cancel; and the webhook
Razorpay calls when a payment or subscription changes.

Buying, cancelling and seeing invoices need `manage_billing` (the workspace owner). Prices are open to every member, so anyone can see
what a plan costs.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from app.billing import invoices
from app.billing import plans as P
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.billing.provider import get_provider
from app.core.auth import get_current_user
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require

router = APIRouter()


@router.get("/plans")
@limiter.limit("60/minute")
async def plans(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """Prices in both currencies, how many founding places are left, and whether checkout is open."""
    from app.billing import plans as P
    from app.shared import entitlements

    await PR.load_book()
    left = await S.founding_left()
    cfg = await invoices.load_settings()
    provider = get_provider()
    table = await entitlements.load_plans()
    # what each plan gives, from the plans table (Ops can change it), with a seat range for the plan picker
    plan_limits = {
        pid: {
            "name": spec.get("name"), "min_seats": spec.get("min_seats"), "max_seats": spec.get("max_seats"),
            "per_seat": bool(spec.get("per_seat")), "limits": P.limits_for(spec),
            "rules": spec.get("limits"),
        }
        for pid, spec in table.items()
    }
    return {
        "plans": plan_limits,
        "founding_left": left,
        "founding_limit": PR.founding_limit(),
        "volume_from_seats": PR.volume_from_seats(),
        "checkout_open": bool(cfg.get("checkout_open")) and provider.configured(),
        "checkout_currencies": list(PR.CHECKOUT_CURRENCIES),
        "mode": provider.mode,
        "prices": {cur: PR.table(cur, founding=left > 0) for cur in PR.CURRENCIES},
        "list_prices": {cur: PR.table(cur) for cur in PR.CURRENCIES},
    }


@router.get("/subscription")
@limiter.limit("60/minute")
async def subscription(request: Request, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """The plan in force, a change waiting for the renewal, an approval the bank is waiting on, and seats in use."""
    from app.billing import lifecycle
    from app.shared import entitlements

    sub = await S.current_for_workspace(ctx.workspace_id)
    scheduled = await S.scheduled_change(ctx.workspace_id, (sub or {}).get("id"))
    approval = await lifecycle.open_approval_for_workspace(ctx.workspace_id)
    ent = await entitlements.entitlements_for(ctx.workspace)
    return {
        "subscription": S.public(sub), "scheduled": S.public(scheduled), "billing_status": ctx.workspace.get("billing_status") or "none",
        "renews_at": (lifecycle.renewal_at(sub).isoformat() if sub and lifecycle.renewal_at(sub) else None),
        "approval": {"token": approval["token"], "amount": approval["amount"], "currency": approval["currency"]} if approval else None,
        "seats_in_use": int(await entitlements.count_usage(ent, "seats")),
        "payment_method": await S.latest_method(ctx.workspace_id),
    }


async def _room_on(ctx: WorkspaceContext, plan: str, seats: int) -> None:
    """A plan can only be chosen when the people and workspaces already on the plan fit in it: unlike accounts or voices, they
    cannot simply stay over the limit."""
    from app.shared import entitlements

    table = await entitlements.load_plans()
    limits = P.limits_for(table[plan], seats)
    ent = await entitlements.entitlements_for(ctx.workspace)
    name = str(table[plan].get("name") or plan.title())
    people = int(await entitlements.count_usage(ent, "seats"))
    if limits.get("seats") is not None and people > int(limits["seats"]):
        message = (
            "Seats in use are the lowest you can go." if plan == "large"
            else f"{people} people use this plan and {name} has {limits['seats']} seat{'s' if limits['seats'] != 1 else ''}. Remove people first, or pick a plan with enough seats."
        )
        raise HTTPException(status_code=400, detail={"code": "billing_refused", "message": message})
    covered = int(await entitlements.count_usage(ent, "owned_workspaces"))
    if limits.get("owned_workspaces") is not None and covered > int(limits["owned_workspaces"]):
        raise HTTPException(status_code=400, detail={"code": "billing_refused", "message": (
            f"This plan covers {covered} workspaces and {name} covers {limits['owned_workspaces']}. Move some workspaces off this plan first."
        )})


class ChangeRequest(BaseModel):
    plan: Literal["single", "duo", "large"]
    period: Literal["monthly", "yearly"]
    seats: int = Field(default=1, ge=1, le=100)


@router.post("/change/quote")
@limiter.limit("60/minute")
async def change_quote(request: Request, body: ChangeRequest, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """What a change costs today and from the renewal, and whether it applies now or at the renewal."""
    from app.billing import lifecycle

    terms = await lifecycle.quote_change(workspace_id=ctx.workspace_id, plan=body.plan, period=body.period, seats=body.seats)
    await _room_on(ctx, body.plan, terms["seats"])
    return terms


@router.post("/change")
@limiter.limit("10/minute")
async def change(request: Request, body: ChangeRequest, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """Open the subscription that takes over: at once for a bigger plan or more seats, at the renewal otherwise."""
    from app.billing import lifecycle
    from app.shared import entitlements

    table = await entitlements.load_plans()
    await _room_on(ctx, body.plan, P.clamp_seats(table[body.plan], body.seats))
    return await lifecycle.start_change(workspace=ctx.workspace, user=ctx.user, plan=body.plan, period=body.period, seats=body.seats)


@router.post("/keep")
@limiter.limit("10/minute")
async def keep(request: Request, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """Keep a plan that was set to end: the same plan again from its renewal, approved once in Razorpay's window."""
    from app.billing import lifecycle

    current = await S.current_for_workspace(ctx.workspace_id)
    if not current:
        raise HTTPException(status_code=404, detail="This workspace has no plan to keep.")
    return await lifecycle.start_change(
        workspace=ctx.workspace, user=ctx.user, plan=current["plan"], period=current["period"], seats=int(current.get("seats") or 1), keep=True,
    )


@router.post("/payment-method")
@limiter.limit("10/minute")
async def change_payment_method(request: Request, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """Pay the same plan another way: approved once with the new card or UPI, it takes over at the renewal and the old one ends then.
    Razorpay cannot swap the method of a UPI or bank mandate in place, so this works the same way for every method."""
    from app.billing import lifecycle

    current = await S.current_for_workspace(ctx.workspace_id)
    if not current:
        raise HTTPException(status_code=404, detail="This workspace has no paid plan.")
    return await lifecycle.start_change(
        workspace=ctx.workspace, user=ctx.user, plan=current["plan"], period=current["period"], seats=int(current.get("seats") or 1),
        new_method=True,
    )


@router.get("/invoices/{invoice_id}")
@limiter.limit("60/minute")
async def get_invoice(request: Request, invoice_id: str, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """One receipt, with how it was paid."""
    doc = await invoices._invoices_col().find_one({"id": invoice_id[:64], "workspace_id": ctx.workspace_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="We couldn't find that receipt.")
    pay = await S.payments_col().find_one({"id": doc.get("payment_id")}, {"_id": 0})
    return {**doc, "status": doc.get("status") or "paid", "paid_with": (S.method_label(pay) or {}).get("label")}


class PauseRequest(BaseModel):
    months: Literal[1, 2]


@router.post("/pause")
@limiter.limit("10/minute")
async def pause(request: Request, body: PauseRequest, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """Pause from the end of the period already paid for, for one or two months. Payments start again by themselves."""
    from app.billing import lifecycle

    return {"subscription": await lifecycle.request_pause(workspace_id=ctx.workspace_id, user_id=ctx.user_id, months=body.months)}


@router.post("/resume")
@limiter.limit("10/minute")
async def resume(request: Request, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    from app.billing import lifecycle

    return {"subscription": await lifecycle.resume_now(workspace_id=ctx.workspace_id, user_id=ctx.user_id)}


@router.get("/coverage")
@limiter.limit("30/minute")
async def coverage(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Which plan could cover a new workspace: each paid workspace this person owns, with how many workspaces it already covers and
    how many it allows, and whether their one free workspace is still free to use."""
    from app.db.mongo import workspaces
    from app.shared import entitlements

    owned = [w async for w in workspaces.find(
        {"owner_id": user["id"], "deleted_at": {"$exists": False}}, {"id": 1, "name": 1, "plan": 1, "billing_group_id": 1},
    )]
    plans = []
    free_used_by = None
    for w in owned:
        if w.get("billing_group_id"):
            continue
        # a plan given free from Ops counts the same as one paid for
        ent = await entitlements.entitlements_for(w["id"])
        if ent.plan == "free":
            free_used_by = free_used_by or w.get("name") or "a workspace"
            continue
        used = len(await entitlements.group_ids(w["id"]))
        plans.append({
            "workspace_id": w["id"], "name": w.get("name") or "", "plan": ent.plan, "plan_name": ent.plan_name,
            "used": used, "allowed": ent.plan_limits.get("owned_workspaces"),
        })
    return {"plans": plans, "free_available": free_used_by is None, "free_used_by": free_used_by}


@router.get("/approval/{token}")
@limiter.limit("30/minute")
async def approval(request: Request, token: str) -> dict:
    """The page a renewal approval link opens. Needs no sign-in: the token itself is the key, and it shows only what is being approved."""
    from app.billing import lifecycle

    doc = await lifecycle.approval_by_token(token[:80])
    if not doc:
        raise HTTPException(status_code=404, detail="This link is not valid.")
    keys = ("workspace_name", "plan", "period", "amount", "currency", "renews_on", "expired", "approved")
    out = {k: doc.get(k) for k in keys}
    out["url"] = None if doc["expired"] or doc["approved"] else doc.get("url")
    return out


class Customer(BaseModel):
    """Who the receipt is made out to. Only the country is needed to pay; the rest is optional (a business adds its name, address and
    GSTIN), and the name and email fall back to the owner's account."""
    name: str = Field(default="", max_length=200)
    email: Optional[str] = Field(default=None, max_length=200)
    address: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    postal_code: str = Field(default="", max_length=20)
    country: str = Field(default="IN", min_length=2, max_length=60)
    # optional: empty, or the 15 letters and digits of a GSTIN
    gstin: Optional[str] = Field(default=None, max_length=15, pattern=r"^([0-9A-Za-z]{15})?$")


class QuoteRequest(BaseModel):
    plan: Literal["single", "duo", "large"]
    period: Literal["monthly", "yearly"]
    seats: int = Field(default=1, ge=1, le=100)
    # the day of the month a monthly plan renews; the 1st unless the owner picks another
    billing_day: int = Field(default=1, ge=1, le=28)
    country: str = Field(default="IN", min_length=2, max_length=60)


@router.post("/quote")
@limiter.limit("60/minute")
async def quote(request: Request, body: QuoteRequest, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """What a plan costs today and at each renewal, worked out the same way checkout does, for the review screen."""
    return await S.get_quote(plan=body.plan, period=body.period, seats=body.seats, billing_day=body.billing_day, country=body.country)


class CheckoutRequest(BaseModel):
    plan: Literal["single", "duo", "large"]
    period: Literal["monthly", "yearly"]
    seats: int = Field(default=1, ge=1, le=100)
    billing_day: int = Field(default=1, ge=1, le=28)
    customer: Customer


@router.post("/checkout")
@limiter.limit("10/minute")
async def checkout(request: Request, body: CheckoutRequest, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """Open the subscription at Razorpay. The browser then shows Razorpay's checkout with what this returns."""
    return await S.start_checkout(
        workspace=ctx.workspace, user=ctx.user, plan=body.plan, period=body.period, seats=body.seats,
        customer=body.customer.model_dump(), billing_day=body.billing_day,
    )


class ConfirmRequest(BaseModel):
    razorpay_payment_id: str = Field(..., min_length=1, max_length=80)
    razorpay_subscription_id: str = Field(..., min_length=1, max_length=80)
    razorpay_signature: str = Field(..., min_length=1, max_length=200)


@router.post("/checkout/confirm")
@limiter.limit("20/minute")
async def confirm(request: Request, body: ConfirmRequest, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    sub = await S.confirm_checkout(
        workspace_id=ctx.workspace_id, payment_id=body.razorpay_payment_id, subscription_id=body.razorpay_subscription_id,
        signature=body.razorpay_signature,
    )
    return {"subscription": sub}


@router.get("/topups")
@limiter.limit("60/minute")
async def topup_offer(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """The credit packs on sale, bought credits not used yet, and whether this workspace may buy (paid plans only)."""
    from app.billing import topups

    return await topups.offer(ctx.workspace)


class TopUpRequest(BaseModel):
    """A pack by its name, or a typed number of credits (priced here, never by the browser)."""
    pack_id: Optional[str] = Field(default=None, min_length=3, max_length=40)
    credits: Optional[int] = Field(default=None, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def _one(self) -> "TopUpRequest":
        if (self.pack_id is None) == (self.credits is None):
            raise ValueError("Choose a pack or a number of credits.")
        return self


@router.get("/topup/quote")
@limiter.limit("120/minute")
async def topup_quote(request: Request, credits: int = Query(..., ge=1, le=1_000_000), ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict:
    """What a typed number of credits costs, and the next band up, so the price shows while the member types."""
    from app.billing import prices as PR

    await PR.load_book()
    try:
        return PR.quote_credits(credits)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/topup")
@limiter.limit("10/minute")
async def topup_start(request: Request, body: TopUpRequest, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """Open the payment for one credit pack."""
    from app.billing import topups

    return await topups.start(workspace=ctx.workspace, user=ctx.user, pack_id=body.pack_id, credits_n=body.credits)


class TopUpConfirm(BaseModel):
    razorpay_payment_id: str = Field(..., min_length=1, max_length=80)
    razorpay_order_id: str = Field(..., min_length=1, max_length=80)
    razorpay_signature: str = Field(..., min_length=1, max_length=200)


@router.post("/topup/confirm")
@limiter.limit("20/minute")
async def topup_confirm(request: Request, body: TopUpConfirm, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    from app.billing import topups

    return {"topup": await topups.confirm(
        workspace_id=ctx.workspace_id, order_id=body.razorpay_order_id, payment_id=body.razorpay_payment_id,
        signature=body.razorpay_signature,
    )}


class CancelRequest(BaseModel):
    """Why they are leaving, if they say: one of the four choices, and an optional note."""
    reason: Optional[Literal["too_expensive", "not_using", "missing_something", "other"]] = None
    note: Optional[str] = Field(default=None, max_length=500)


@router.post("/cancel")
@limiter.limit("10/minute")
async def cancel(request: Request, body: Optional[CancelRequest] = None, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    """Cancel at the end of the period already paid for. Nothing is refunded and nothing is deleted."""
    from app.billing import lifecycle

    sub = await S.cancel_at_period_end(workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    if body and (body.reason or body.note):
        await lifecycle.record_leaving_reason(workspace_id=ctx.workspace_id, user_id=ctx.user_id, reason=body.reason, note=body.note)
    return {"subscription": sub}


@router.get("/invoices")
@limiter.limit("60/minute")
async def list_invoices(request: Request, ctx: WorkspaceContext = Depends(require("manage_billing"))) -> dict:
    return {"invoices": await invoices.list_for_workspace(ctx.workspace_id)}


@router.post("/webhooks/razorpay")
@limiter.limit("600/minute")
async def razorpay_webhook(request: Request) -> dict:
    """Razorpay's message about a payment or subscription. The signature is checked on the raw body, the message is stored once, and
    it is applied in the background, so this answers at once."""
    body = await request.body()
    ok = await S.receive_webhook(
        body, request.headers.get("x-razorpay-signature", ""), request.headers.get("x-razorpay-event-id", ""),
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Signature does not match.")
    return {"ok": True}
