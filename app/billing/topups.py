"""Credit packs: a paid plan buys more credits once its monthly credits run short.

A pack is one payment, not a subscription: Ekavo opens an order at the provider for the pack's price in the price book, the member
pays in the same window as plans, and the credits are added only once the provider says the payment was captured (from the
confirm call or the provider's message, whichever comes first, and only once). Bought credits last 12 months, or until the paid
plan ends, and are spent after the month's own credits (`app.billing.credits`). A refund ends the pack's unused credits.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from app.shared.activity.i18n import phrase
from app.billing import credits, invoices
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.billing.provider import PaymentProvider, ProviderError, get_provider
from app.db.mongo import get_db, workspaces

logger = logging.getLogger(__name__)

# The plan states in which a workspace may buy packs: a plan it pays for now, including a renewal being retried.
BUYING_STATES = ("active", "past_due", "limited")


def topups_col():
    return get_db()["billing_topups"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": "topup_refused", "message": message})


def pack_label(credits_n: int) -> str:
    return f"Ekavo credits, {credits_n:,}"


async def _payer(workspace: dict[str, Any]) -> dict[str, Any]:
    """The workspace that pays for the plan: itself, or the one whose plan it shares."""
    group = workspace.get("billing_group_id")
    if group and group != workspace.get("id"):
        return await workspaces.find_one({"id": group}, {"_id": 0}) or workspace
    return workspace


async def can_buy(workspace: dict[str, Any]) -> tuple[bool, Optional[str]]:
    """Whether this workspace may buy a pack now, and if not, why, in plain words."""
    payer = await _payer(workspace)
    if not PR.is_paid(str(payer.get("plan") or "free")):
        return False, "Credit packs are for paid plans. Upgrade to get more credits each month."
    if str(payer.get("billing_status") or "") not in BUYING_STATES:
        return False, "Credit packs can be bought while your plan is running."
    return True, None


async def offer(workspace: dict[str, Any]) -> dict[str, Any]:
    """The packs on sale, what the workspace has bought and not used, and whether it may buy."""
    await PR.load_book()
    ok, why = await can_buy(workspace)
    pool = await credits.pool_for(workspace["id"])
    left, until = await credits.bought_left(pool.id)
    return {
        "packs": [
            {"id": p["id"], "credits": p["credits"], "amount": p["prices"]["INR"], "currency": "INR",
             "label": PR.format_amount(p["prices"]["INR"], "INR")}
            for p in PR.packs() if p["prices"].get("INR")
        ],
        "custom": PR.custom_credits() if PR.custom_credits()["on"] else None,
        "can_buy": ok, "reason": why, "bought_left": left, "bought_until": until.isoformat() if until else None,
        "lasts_months": credits.BOUGHT_MONTHS,
    }


async def start(
    *, workspace: dict[str, Any], user: dict[str, Any], pack_id: Optional[str] = None, credits_n: Optional[int] = None,
    provider: Optional[PaymentProvider] = None,
) -> dict[str, Any]:
    """Open the provider's order for one pack, or for a typed number of credits priced on the server, and return what the browser's
    payment window needs."""
    provider = provider or get_provider()
    await PR.load_book()
    cfg = await invoices.load_settings()
    if not cfg.get("checkout_open"):
        raise _refuse(403, "Payments are not open yet. You'll hear from us as soon as they are.")
    if not provider.configured():
        raise _refuse(503, "Payments are not set up yet. Try again later.")
    if provider.mode == "live" and not invoices.seller_ready(cfg):
        raise _refuse(503, "Payments are not ready yet. Try again later.")
    ok, why = await can_buy(workspace)
    if not ok:
        raise _refuse(403, why or "Credit packs can't be bought right now.")
    if credits_n is not None:
        try:
            quote = PR.quote_credits(credits_n)
        except ValueError as exc:
            raise _refuse(400, str(exc)) from exc
        pack = {"id": "custom", "credits": credits_n}
        amount = int(quote["amount"])
    else:
        pack = PR.pack(pack_id or "")
        if not pack or not pack["prices"].get("INR"):
            raise _refuse(404, "That pack is no longer on sale. Pick another.")
        amount = int(pack["prices"]["INR"])
    pool = await credits.pool_for(workspace["id"])
    topup_id = uuid4().hex
    try:
        order = await provider.create_order(
            amount=amount, currency="INR", receipt=f"topup-{topup_id[:24]}",
            notes={"workspace_id": workspace["id"], "topup_id": topup_id, "pack": pack["id"], "credits": str(pack["credits"])},
        )
    except ProviderError as exc:
        raise _refuse(502, exc.message) from exc
    now = _now()
    payer = await _payer(workspace)
    sub = await S.subs_col().find_one({"workspace_id": payer["id"]}, {"_id": 0, "customer": 1}, sort=[("created_at", -1)])
    doc = {
        "id": topup_id, "workspace_id": workspace["id"], "pool_id": pool.id, "user_id": user["id"], "pack_id": pack["id"],
        "credits": int(pack["credits"]), "amount": amount, "currency": "INR", "order_id": str(order.get("id")),
        "status": "created", "mode": provider.mode, "customer": (sub or {}).get("customer") or {"name": user.get("name", ""), "email": user.get("email", "")},
        "created_at": now, "updated_at": now,
    }
    await topups_col().insert_one(dict(doc))
    return {
        "topup": {"id": topup_id, "credits": doc["credits"], "amount": amount, "currency": "INR"},
        "checkout": {
            "key": provider.public_key, "order_id": doc["order_id"], "amount": amount, "currency": "INR", "name": "Ekavo",
            "description": f"{doc['credits']:,} credits",
            "prefill": {"name": str(doc["customer"].get("name") or ""), "email": str(doc["customer"].get("email") or "")},
            "notes": {"workspace_id": workspace["id"], "topup_id": topup_id}, "mode": provider.mode,
        },
    }


async def confirm(
    *, workspace_id: str, order_id: str, payment_id: str, signature: str, provider: Optional[PaymentProvider] = None,
) -> dict[str, Any]:
    """What the browser reports when the payment window closes. The signature proves it came from the provider; the payment is then
    read from the provider itself, so credits are only ever added for money really taken."""
    provider = provider or get_provider()
    topup = await topups_col().find_one({"workspace_id": workspace_id, "order_id": order_id}, {"_id": 0})
    if not topup:
        raise HTTPException(status_code=404, detail="We couldn't find that payment. Try again from Credits.")
    if not provider.verify_order_payment(order_id=order_id, payment_id=payment_id, signature=signature):
        await S._reject("topup_signature", {"workspace_id": workspace_id, "order_id": order_id})
        raise HTTPException(status_code=400, detail="We couldn't confirm that payment. If money left your account, it will be returned.")
    try:
        payment = await provider.fetch_payment(payment_id)
    except ProviderError:
        # the provider's message brings the same news a moment later
        return public(topup)
    await apply_payment(payment, provider=provider)
    return public(await topups_col().find_one({"id": topup["id"]}, {"_id": 0}) or topup)


async def apply_payment(payment: dict[str, Any], provider: Optional[PaymentProvider] = None) -> str:
    """A payment for a pack's order: capture it if it is only authorised, then add the credits once. Returns what happened."""
    order_id = str(payment.get("order_id") or "")
    topup = await topups_col().find_one({"order_id": order_id}, {"_id": 0}) if order_id else None
    if not topup:
        return "unmatched"
    if int(payment.get("amount") or 0) != int(topup["amount"]) or str(payment.get("currency") or "") != topup["currency"]:
        logger.warning("Top-up %s paid %s %s, expected %s", topup["id"], payment.get("amount"), payment.get("currency"), topup["amount"])
        await topups_col().update_one({"id": topup["id"]}, {"$set": {"status": "amount_mismatch", "updated_at": _now()}})
        return "mismatch"
    status = str(payment.get("status") or "")
    if status == "authorized":
        provider = provider or get_provider()
        try:
            payment = await provider.capture_payment(str(payment["id"]), amount=int(topup["amount"]), currency=topup["currency"])
        except ProviderError as exc:
            logger.warning("Capturing top-up %s failed: %s", topup["id"], exc.message)
            return "not_captured"
        status = str(payment.get("status") or "")
    if status != "captured":
        if status == "failed":
            await S.record_payment(None, {**payment, "currency": topup["currency"]}, "topup.failed")
        return status or "unknown"
    claimed = await topups_col().find_one_and_update(
        {"id": topup["id"], "status": {"$in": ["created", "authorized"]}},
        {"$set": {"status": "paid", "payment_id": str(payment["id"]), "paid_at": _now(), "updated_at": _now()}},
    )
    if not claimed:
        return "already"
    await S.payments_col().update_one(
        {"id": str(payment["id"])},
        {"$set": {
            "id": str(payment["id"]), "subscription_id": None, "topup_id": topup["id"], "workspace_id": topup["workspace_id"],
            "amount": int(payment.get("amount") or 0), "currency": topup["currency"], "status": "captured",
            "method": payment.get("method"), "event": "topup.paid", "at": S._ts(payment.get("created_at")) or _now(), "mode": topup.get("mode"),
            "card_last4": (payment.get("card") or {}).get("last4"), "card_network": (payment.get("card") or {}).get("network"),
            "vpa": payment.get("vpa"), "bank": payment.get("bank"), "wallet": payment.get("wallet"),
        }},
        upsert=True,
    )
    label = pack_label(int(topup["credits"]))
    await credits.add_bought(topup["workspace_id"], topup_id=topup["id"], credits=int(topup["credits"]), label=label)
    await invoices.issue_for_payment(
        {"workspace_id": topup["workspace_id"], "id": None, "mode": topup.get("mode"), "customer": topup.get("customer") or {}},
        {**payment, "currency": topup["currency"]}, label=label,
    )
    await _tell(topup)
    return "applied"


async def _tell(topup: dict[str, Any]) -> None:
    from app.shared.activity.projector import record_system

    try:
        await record_system(
            workspace_id=topup["workspace_id"], key=f"topup:{topup['id']}", actor_name="Ekavo", category="billing",
            title=phrase("{count} credits added", count=f"{int(topup['credits']):,}"), description="They are used once this month's credits run out, and last 12 months.",
            status="success", href="/dashboard/settings?tab=billing",
        )
    except Exception as exc:  # noqa: BLE001 - a missed note never undoes the purchase
        logger.warning("Top-up note for %s failed: %s", topup["id"], exc)


async def on_refund(payment_id: str, refunded: int) -> None:
    """A pack's payment was refunded (in part or in full): the same share of its credits ends, never more than are left unused."""
    topup = await topups_col().find_one({"payment_id": payment_id}, {"_id": 0})
    if not topup or not topup.get("amount"):
        return
    owed = round(int(topup["credits"]) * min(refunded, int(topup["amount"])) / int(topup["amount"]))
    already = int(topup.get("clawed_back") or 0)
    if owed <= already:
        return
    ended = await credits.end_bought(topup["pool_id"], why="refunded", lot_id=topup["id"], limit=owed - already, kind="clawback")
    # what was owed but already spent is recorded, so Ops sees it
    await topups_col().update_one({"id": topup["id"]}, {"$set": {"clawed_back": owed, "spent_before_refund": max(0, owed - already - ended),
                                                               "updated_at": _now()}})


def public(topup: dict[str, Any]) -> dict[str, Any]:
    return {k: topup.get(k) for k in ("id", "credits", "amount", "currency", "status", "created_at", "paid_at")}


async def create_indexes() -> None:
    await topups_col().create_index("id", unique=True)
    await topups_col().create_index("order_id")
    await topups_col().create_index("payment_id")
    await topups_col().create_index([("workspace_id", 1), ("created_at", -1)])
