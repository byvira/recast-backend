"""Paid subscriptions: checkout, the provider's webhooks, and putting a workspace on the plan it pays for.

Ekavo keeps a mirror of every subscription (`billing_subscriptions`) and payment (`billing_payments`); the provider is the source of truth
for money. A workspace's plan follows its subscription: `plan`, `plan_seats` and `billing_status` on the workspace are written only here,
so entitlements and credits read one answer.

Webhooks are stored first (`billing_webhook_events`, one row per event id, so a repeat is ignored), answered at once, and processed
after. An event older than the newest one already applied to a subscription never moves it backwards.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.billing import invoices
from app.billing import plans as P
from app.billing import prices as PR
from app.billing.provider import PaymentProvider, ProviderError, get_provider
from app.db.mongo import get_db, workspaces

logger = logging.getLogger(__name__)

# What the provider calls a subscription's states.
LIVE_STATES = ("created", "authenticated", "active", "pending", "halted", "paused")
# States in which the workspace keeps its paid plan. "pending" and "halted" are a failed renewal being retried: the plan stays while
# the grace period runs.
PAID_STATES = ("active", "pending", "halted")
ENDED_STATES = ("cancelled", "completed", "expired")
# How many periods a subscription runs before it has to be renewed by the member: ten years.
TOTAL_COUNT = {"monthly": 120, "yearly": 10}
# A checkout that was opened but never paid is let go after a day.
ABANDON_AFTER_HOURS = 24
# How long a worker applying a webhook keeps it to itself before another may pick it up.
CLAIM_FOR = timedelta(minutes=5)
# After a renewal fails, everything keeps working this long; then the workspace is limited until it pays.
GRACE = timedelta(days=7)
# Banks ask the customer to approve each automatic payment above this amount (paise).
AUTO_DEBIT_LIMIT = 1_500_000


def subs_col():
    return get_db()["billing_subscriptions"]


def provider_plans_col():
    return get_db()["billing_provider_plans"]


def events_col():
    return get_db()["billing_webhook_events"]


def payments_col():
    return get_db()["billing_payments"]


def rejects_col():
    return get_db()["billing_webhook_rejects"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: Any) -> Optional[datetime]:
    """A provider timestamp (seconds since 1970) as a date."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None
    except (TypeError, ValueError, OSError):
        return None


def public(sub: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A subscription as the member's screens see it: no provider ids beyond what checkout needs."""
    if not sub:
        return None
    keys = ("id", "plan", "period", "currency", "seats", "founding", "unit_amount", "amount", "status", "cancel_at_period_end",
            "billing_day", "first_amount", "first_renewal", "pause", "replaces", "change_kind", "starts_now", "grace_until",
            "payment_method",
            "current_start", "current_end", "charge_at", "mode", "created_at", "customer")
    out = {k: sub.get(k) for k in keys}
    out["amount_label"] = PR.format_amount(int(sub.get("amount") or 0), sub.get("currency") or "INR")
    return out


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_paid(sub: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether a subscription gives its workspace the paid plan now.

    A monthly plan waiting for its billing day is "authenticated" at the provider with its first days already paid, so it counts; so
    does a change that applies at once and was paid for. A change that waits for the renewal does not count until it starts. A plan
    paused at the end of its period keeps working until that period ends."""
    status = str(sub.get("status"))
    if status in PAID_STATES:
        return True
    if status == "authenticated":
        return bool(sub.get("starts_now", sub.get("anchored")))
    if status == "paused":
        end = _aware(sub.get("current_end"))
        return bool(end and end > (now or _now()))
    return False


# ── reading ────────────────────────────────────────────────────────────────

async def current_for_workspace(workspace_id: str) -> Optional[dict[str, Any]]:
    """The workspace's subscription in force now; else its newest live one (a change waiting for the renewal, a paused plan); else its
    most recent one."""
    live = [s async for s in subs_col().find(
        {"workspace_id": workspace_id, "status": {"$in": [s for s in LIVE_STATES if s != "created"]}}, {"_id": 0},
    ).sort("created_at", -1)]
    now = _now()
    in_force = [s for s in live if is_paid(s, now)]
    if in_force:
        return in_force[0]
    return live[0] if live else await subs_col().find_one({"workspace_id": workspace_id}, {"_id": 0}, sort=[("created_at", -1)])


async def scheduled_change(workspace_id: str, current_id: Optional[str]) -> Optional[dict[str, Any]]:
    """A change set up to take over at the renewal: a smaller plan, fewer seats, a switch to yearly, or keeping a cancelled plan."""
    return await subs_col().find_one(
        {"workspace_id": workspace_id, "replaces": current_id, "starts_now": False, "status": {"$in": ["authenticated", "active"]}},
        {"_id": 0}, sort=[("created_at", -1)],
    ) if current_id else None


async def founding_taken() -> int:
    """Workspaces holding a founding price: paying ones, and checkouts opened in the last day that may still be paid. A workspace
    counts once, however many subscriptions a plan change gave it."""
    recent = _now() - timedelta(hours=ABANDON_AFTER_HOURS)
    ids = await subs_col().distinct("workspace_id", {"founding": True, "$or": [
        {"status": {"$in": list(PAID_STATES) + ["paused", "authenticated"]}},
        {"status": "created", "created_at": {"$gt": recent}},
    ]})
    return len(ids)


async def founding_left() -> int:
    await PR.load_book()
    return max(0, PR.founding_limit() - await founding_taken())


def locks_col():
    return get_db()["billing_locks"]


# A turn a crashed checkout never gave back runs out after this long: longer than the two provider calls a checkout makes can take.
FOUNDING_TURN_SECONDS = 90
# How long a checkout waits for its turn before asking the person to try again.
FOUNDING_WAIT_SECONDS = 30


@asynccontextmanager
async def founding_turn() -> AsyncIterator[None]:
    """One checkout at a time, across every server: deciding on a founding price and saving the checkout happen inside one turn, so
    the count the next checkout reads already includes this one."""
    me = uuid4().hex
    deadline = time.monotonic() + FOUNDING_WAIT_SECONDS
    while True:
        now = _now()
        try:
            await locks_col().update_one(
                {"_id": "founding", "$or": [{"until": None}, {"until": {"$lt": now}}]},
                {"$set": {"holder": me, "until": now + timedelta(seconds=FOUNDING_TURN_SECONDS)}},
                upsert=True,
            )
            break
        except DuplicateKeyError:  # another checkout holds the turn
            if time.monotonic() > deadline:
                raise _checkout_refusal(503, "Many people are paying right now. Try again in a moment.")
            await asyncio.sleep(0.2)
    try:
        yield
    finally:
        await locks_col().delete_one({"_id": "founding", "holder": me})


# ── provider plans ─────────────────────────────────────────────────────────

async def provider_plan_id(provider: PaymentProvider, plan: str, currency: str, period: str, amount: int) -> str:
    """The provider's plan for this exact price, made the first time it is needed and reused after."""
    key = f"{provider.name}:{provider.mode}:{PR.price_key(plan, currency, period, amount)}"  # type: ignore[arg-type]
    saved = await provider_plans_col().find_one({"_id": key})
    if saved:
        return str(saved["provider_plan_id"])
    unit = "a seat" if plan == "large" else "a month" if period == "monthly" else "a year"
    pid = await provider.create_plan(
        period=period, amount=amount, currency=currency, name=f"Ekavo {plan.title()} ({period})",
        description=f"{PR.format_amount(amount, currency)} {unit}", notes={"price_key": key},  # type: ignore[arg-type]
    )
    try:
        await provider_plans_col().insert_one({"_id": key, "provider_plan_id": pid, "created_at": _now()})
    except DuplicateKeyError:
        # two checkouts made the same plan at once; keep the first, the second plan is simply never used
        saved = await provider_plans_col().find_one({"_id": key})
        return str(saved["provider_plan_id"]) if saved else pid
    return pid


# ── checkout ───────────────────────────────────────────────────────────────

def _checkout_refusal(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": "checkout_refused", "message": message})


# A monthly plan renews at this hour (UTC) of its billing day: past midnight in India, and still that day everywhere checkout is open.
RENEW_HOUR_UTC = 6


def _renew_moment(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, RENEW_HOUR_UTC, tzinfo=timezone.utc).timestamp())


# Paid plans are open in India only, so "today" for a quote is the date there.
INDIA_OFFSET = timedelta(hours=5, minutes=30)


def _today() -> date:
    return (_now() + INDIA_OFFSET).date()


async def get_quote(*, plan: str, period: str, seats: int, billing_day: int, country: str) -> dict[str, Any]:
    """What checkout would charge today and at each renewal, worked out the same way checkout does."""
    from app.shared import entitlements

    if not PR.is_paid(plan) or period not in PR.PERIODS:
        raise _checkout_refusal(400, "Choose Single, Duo or Large, monthly or yearly.")
    if billing_day not in PR.BILLING_DAYS:
        raise _checkout_refusal(400, "Choose a billing day from 1 to 28.")
    seats = P.clamp_seats((await entitlements.load_plans())[plan], seats)
    currency = PR.currency_for_country(country)
    founding = await founding_left() > 0
    q = PR.quote(plan, currency, period, seats=seats, founding=founding, today=_today(), billing_day=billing_day)  # type: ignore[arg-type]
    return {**q, "plan": plan, "period": period, "seats": seats}


async def start_checkout(
    *, workspace: dict[str, Any], user: dict[str, Any], plan: str, period: str, seats: int, customer: dict[str, Any],
    billing_day: int = 1, provider: Optional[PaymentProvider] = None,
) -> dict[str, Any]:
    """Open a subscription at the provider and return what the browser's checkout needs. The workspace changes plan only once the
    provider confirms the first payment.

    A monthly plan starts on its billing day (the 1st unless the owner picked another): the days until then are charged up front with
    the mandate, and the regular charge starts on that day. A yearly plan is charged in full at once."""
    provider = provider or get_provider()
    cfg = await invoices.load_settings()
    if not cfg.get("checkout_open"):
        raise _checkout_refusal(403, "Paid plans are not open yet. You'll hear from us as soon as they are.")
    if not provider.configured():
        raise _checkout_refusal(503, "Payments are not set up yet. Try again later.")
    if provider.mode == "live" and not invoices.seller_ready(cfg):
        raise _checkout_refusal(503, "Payments are not ready yet. Try again later.")
    if not PR.is_paid(plan) or period not in PR.PERIODS:
        raise _checkout_refusal(400, "Choose Single, Duo or Large, monthly or yearly.")
    if billing_day not in PR.BILLING_DAYS:
        raise _checkout_refusal(400, "Choose a billing day from 1 to 28.")
    currency = PR.currency_for_country(customer.get("country"))
    if currency not in PR.CHECKOUT_CURRENCIES:
        raise _checkout_refusal(400, "Paid plans are open in India first. Join the waiting list and we'll tell you when they open where you are.")

    from app.shared import entitlements

    table = await entitlements.load_plans()
    spec = table[plan]
    seats = P.clamp_seats(spec, seats)
    if workspace.get("billing_group_id"):
        raise _checkout_refusal(400, "This workspace shares another workspace's plan. Change the plan from that workspace.")

    live = await subs_col().find_one({"workspace_id": workspace["id"], "status": {"$in": [s for s in LIVE_STATES if s != "created"]}})
    if live:
        raise _checkout_refusal(409, "This workspace already has a paid plan. Change it from Billing.")
    # an earlier checkout that was opened and never paid is replaced; one that was paid after all stops this one
    async for stale in subs_col().find({"workspace_id": workspace["id"], "status": "created"}):
        if not await _let_go(provider, stale):
            raise _checkout_refusal(409, "Your earlier payment went through. Give it a minute, then check Billing.")

    # one checkout at a time decides on a founding price and saves its checkout, so the last places are never given twice
    async with founding_turn():
        # an earlier unpaid checkout of this workspace no longer holds a founding place
        founding = await founding_left() > 0
        unit = PR.unit_amount(plan, currency, period, seats=seats, founding=founding)  # type: ignore[arg-type]
        quantity = PR.quantity_for(plan, seats)
        amount = unit * quantity
        q = PR.quote(plan, currency, period, seats=seats, founding=founding, today=_today(), billing_day=billing_day)  # type: ignore[arg-type]
        anchored = period == "monthly"
        start_at = _renew_moment(date.fromisoformat(q["renews_on"])) if anchored else None
        upfront = {"name": f"Ekavo {plan.title()} until {date.fromisoformat(q['renews_on']).strftime('%d %b')}", "amount": q["today"], "currency": currency} if anchored else None
        try:
            plan_id = await provider_plan_id(provider, plan, currency, period, unit)
            sub_ref = uuid4().hex
            created = await provider.create_subscription(
                plan_id=plan_id, quantity=quantity, total_count=TOTAL_COUNT[period],
                notes={"workspace_id": workspace["id"], "sub_ref": sub_ref, "plan": plan, "period": period},
                start_at=start_at, upfront=upfront,
            )
        except ProviderError as exc:
            raise _checkout_refusal(502, exc.message) from exc

        now = _now()
        doc = {
            "id": sub_ref, "workspace_id": workspace["id"], "owner_user_id": user["id"], "plan": plan, "period": period,
            "currency": currency, "seats": seats, "founding": founding, "unit_amount": unit, "amount": amount,
            "billing_day": billing_day if anchored else None, "anchored": anchored, "first_amount": q["today"],
            "first_renewal": q["renews_on"],
            "provider": provider.name, "mode": provider.mode, "provider_plan_id": plan_id,
            "provider_subscription_id": str(created.get("id")), "short_url": created.get("short_url"),
            "status": str(created.get("status") or "created"), "cancel_at_period_end": False,
            "current_start": None, "current_end": None, "charge_at": None, "provider_seen_at": None,
            "customer": _clean_customer(customer, user), "created_at": now, "updated_at": now,
        }
        await subs_col().insert_one(dict(doc))
    await _audit("checkout_opened", doc, by=user["id"])
    return {
        "subscription": public(doc),
        "quote": {**q, "plan": plan, "period": period, "seats": seats},
        "checkout": {
            "key": provider.public_key, "subscription_id": doc["provider_subscription_id"], "name": "Ekavo",
            "description": f"{plan.title()}, {period}" + (f", {seats} seats" if plan == "large" else ""),
            "prefill": {"name": doc["customer"].get("name", ""), "email": doc["customer"].get("email", "")},
            "notes": {"workspace_id": workspace["id"], "sub_ref": sub_ref},
            "mode": provider.mode,
        },
    }


def _clean_customer(customer: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    keep = ("name", "email", "address", "city", "state", "postal_code", "country", "gstin")
    out = {k: str(customer.get(k) or "").strip()[:200] for k in keep}
    out["email"] = out["email"] or str(user.get("email") or "")
    out["name"] = out["name"] or str(user.get("name") or "")
    out["country"] = (out["country"] or "IN").upper()
    out["gstin"] = out["gstin"].upper()
    return out


async def confirm_checkout(
    *, workspace_id: str, payment_id: str, subscription_id: str, signature: str, provider: Optional[PaymentProvider] = None,
) -> dict[str, Any]:
    """What the browser reports when checkout closes. The signature proves it came from the provider; the subscription's real state is
    then read from the provider itself, so a screen can never put a workspace on a plan it did not pay for."""
    provider = provider or get_provider()
    sub = await subs_col().find_one({"workspace_id": workspace_id, "provider_subscription_id": subscription_id}, {"_id": 0})
    if not sub:
        raise HTTPException(status_code=404, detail="We couldn't find that checkout. Try again from the plan picker.")
    if not provider.verify_checkout(payment_id=payment_id, subscription_id=subscription_id, signature=signature):
        await _reject("checkout_signature", {"workspace_id": workspace_id, "subscription_id": subscription_id})
        raise HTTPException(status_code=400, detail="We couldn't confirm that payment. If money left your account, it will be returned.")
    try:
        entity = await provider.fetch_subscription(subscription_id)
    except ProviderError:
        # the webhook brings the same news a moment later
        return public(sub) or {}
    updated = await sync(entity, seen_at=None, source="checkout")
    return public(updated or sub) or {}


async def renewing(workspace_id: str) -> bool:
    """Whether the workspace has a subscription that will charge again: in force or waiting to start, and not set to end."""
    return bool(await subs_col().find_one(
        {"workspace_id": workspace_id, "status": {"$in": ["authenticated", *PAID_STATES, "paused"]}, "cancel_at_period_end": {"$ne": True}},
        {"_id": 1},
    ))


def not_started(sub: dict[str, Any]) -> bool:
    """A change approved to take over at the renewal that has not taken over yet: nothing has been charged on it."""
    return bool(sub.get("replaces")) and not sub.get("starts_now") and str(sub.get("status")) in ("created", "authenticated")


async def cancel_at_period_end(*, workspace_id: str, user_id: str, provider: Optional[PaymentProvider] = None) -> dict[str, Any]:
    """Stop the plan at the end of the period paid for. Every subscription of the workspace is stopped: the plan in force ends at its
    renewal, and a change waiting to take over at that renewal is cancelled now, so nothing is charged after cancelling."""
    provider = provider or get_provider()
    current = await current_for_workspace(workspace_id)
    if current and current.get("status") == "paused" and not is_paid(current):
        # paused after its paid period: nothing is left to run out, so it ends now and never resumes
        try:
            entity = await provider.cancel_subscription(current["provider_subscription_id"], at_cycle_end=False)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        await subs_col().update_one({"id": current["id"]}, {"$set": {"pause": None, "updated_at": _now()}})
        await sync(entity, seen_at=None, source="cancel")
        await _audit("cancel_requested", current, by=user_id, extra={"while": "paused"})
        return public(await subs_col().find_one({"id": current["id"]}, {"_id": 0})) or {}
    if not current or not is_paid(current):
        raise HTTPException(status_code=404, detail="This workspace has no paid plan to cancel.")
    others = [s async for s in subs_col().find(
        {"workspace_id": workspace_id, "id": {"$ne": current["id"]}, "status": {"$in": list(LIVE_STATES)}}, {"_id": 0},
    )]
    for other in others:
        if other.get("status") == "created":
            await _let_go(provider, other)
        elif not_started(other):
            try:
                entity = await provider.cancel_subscription(other["provider_subscription_id"], at_cycle_end=False)
            except ProviderError as exc:
                raise HTTPException(status_code=502, detail=exc.message) from exc
            await sync(entity, seen_at=None, source="cancel")
        elif not other.get("cancel_at_period_end"):
            try:
                await provider.cancel_subscription(other["provider_subscription_id"], at_cycle_end=True)
            except ProviderError as exc:
                raise HTTPException(status_code=502, detail=exc.message) from exc
            await subs_col().update_one({"id": other["id"]}, {"$set": {"cancel_at_period_end": True, "updated_at": _now()}})
    if not current.get("cancel_at_period_end"):
        try:
            entity = await provider.cancel_subscription(current["provider_subscription_id"], at_cycle_end=True)
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        await subs_col().update_one({"id": current["id"]}, {"$set": {"cancel_at_period_end": True, "pause": None, "updated_at": _now()}})
        await sync(entity, seen_at=None, source="cancel")
    await _audit("cancel_requested", current, by=user_id, extra={"also": [o["id"] for o in others]})
    return public(await subs_col().find_one({"id": current["id"]}, {"_id": 0})) or {}


async def _let_go(provider: PaymentProvider, stale: dict[str, Any]) -> bool:
    """Let go of a checkout that was opened and not paid. The provider is asked first: one that was paid after all (its message has
    not arrived yet) is kept and brought in step instead. Returns True when it was let go."""
    try:
        entity = await provider.fetch_subscription(stale["provider_subscription_id"])
    except ProviderError as exc:
        logger.warning("Could not read checkout %s before letting it go: %s", stale.get("provider_subscription_id"), exc.message)
        raise HTTPException(status_code=502, detail="We couldn't reach the payment service. Try again in a minute.") from exc
    if str(entity.get("status") or "created") != "created":
        await sync(entity, seen_at=None, source="let_go")
        return False
    try:
        await provider.cancel_subscription(stale["provider_subscription_id"], at_cycle_end=False)
    except ProviderError as exc:
        logger.info("Could not cancel unused checkout %s: %s", stale.get("provider_subscription_id"), exc.message)
    await subs_col().update_one({"id": stale["id"], "status": "created"}, {"$set": {"status": "abandoned", "updated_at": _now()}})
    return True


# ── keeping the mirror in step ─────────────────────────────────────────────

_STATE_ORDER = {"created": 0, "authenticated": 1, "active": 2, "pending": 2, "halted": 2, "paused": 2, "cancelled": 3, "completed": 3, "expired": 3}


async def sync(entity: dict[str, Any], *, seen_at: Optional[datetime], source: str) -> Optional[dict[str, Any]]:
    """Copy the provider's view of a subscription onto the mirror and the workspace. Returns the updated mirror, or None when the
    subscription is not one of ours.

    `seen_at` is the provider's own time on a webhook. A subscription read straight from the provider passes None: it is the
    provider's state now, and Ekavo's clock is never compared with the provider's."""
    pid = str(entity.get("id") or "")
    sub = await subs_col().find_one({"provider_subscription_id": pid}, {"_id": 0}) if pid else None
    if not sub:
        return None
    status = str(entity.get("status") or sub.get("status"))
    last_seen = sub.get("provider_seen_at")
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    older = seen_at is not None and last_seen is not None and seen_at < last_seen
    # an event that arrives late never moves a subscription back (the provider's times are whole seconds, so a move back needs a
    # strictly newer event), and an ended one never comes back to life
    backwards = _STATE_ORDER.get(status, 0) < _STATE_ORDER.get(str(sub.get("status")), 0)
    stale_move_back = backwards and seen_at is not None and (last_seen is None or seen_at <= last_seen)
    if older or stale_move_back or (backwards and str(sub.get("status")) in ENDED_STATES):
        return sub
    changes: dict[str, Any] = {
        "status": status, "updated_at": _now(),
        "current_start": _ts(entity.get("current_start")) or sub.get("current_start"),
        "current_end": _ts(entity.get("current_end")) or sub.get("current_end"),
        "charge_at": _ts(entity.get("charge_at")),
        "paid_count": entity.get("paid_count", sub.get("paid_count")),
    }
    if entity.get("quantity") is not None and sub.get("plan") == "large":
        changes["seats"] = int(entity["quantity"])
    if entity.get("payment_method"):
        changes["payment_method"] = str(entity["payment_method"])
    # a failed renewal starts seven days of grace with everything working; paying ends it
    if status in ("pending", "halted") and not sub.get("grace_until"):
        changes["grace_until"] = _now() + GRACE
    elif status == "active" and sub.get("grace_until"):
        changes["grace_until"] = None
    if seen_at is not None:
        changes["provider_seen_at"] = max(seen_at, last_seen) if last_seen else seen_at
    await subs_col().update_one({"id": sub["id"]}, {"$set": changes})
    sub = {**sub, **changes}
    await apply_to_workspace(sub)
    return sub


def billing_status_of(sub: dict[str, Any], now: Optional[datetime] = None) -> str:
    """How a paid plan stands: active, past due (a renewal is being retried, everything still works), or limited (the grace period
    ran out: no new AI work until it is paid; editing, scheduling and publishing what is ready still work)."""
    if str(sub.get("status")) not in ("pending", "halted"):
        return "active"
    grace = _aware(sub.get("grace_until"))
    return "limited" if grace and grace <= (now or _now()) else "past_due"


async def apply_to_workspace(sub: dict[str, Any]) -> None:
    await apply_for_workspace(sub["workspace_id"])


def _size_for(plan: str) -> dict[str, Any]:
    """The team size a plan gives (Free and Single one person, Duo two, Large a team), so the size always follows what is paid for."""
    from app.core.tiers import TIER_DEFAULTS
    from app.models.workspace import WorkspaceTier

    tier = WorkspaceTier({"duo": "duo", "large": "large"}.get(plan, "single"))
    return {"tier": tier.value, "tier_config": TIER_DEFAULTS[tier].model_dump()}


async def apply_for_workspace(workspace_id: str) -> None:
    """Put the workspace on the plan it pays for now. A workspace can hold two subscriptions for a while (one ending at the renewal and
    the change taking over), so every one of them is looked at and the newest one in force wins. With none in force it is on Free:
    paused when its latest plan is paused, ended otherwise."""
    now = _now()
    subs = [s async for s in subs_col().find(
        {"workspace_id": workspace_id, "status": {"$nin": ["created", "abandoned"]}}, {"_id": 0},
    ).sort("created_at", -1)]
    if not subs:
        return
    in_force = [s for s in subs if is_paid(s, now)]
    if in_force:
        sub = in_force[0]
        # a monthly plan's credits start again on its billing day; a yearly plan's credits keep the calendar month
        values: dict[str, Any] = {
            "plan": sub["plan"], "plan_seats": int(sub.get("seats") or 1), "billing_status": billing_status_of(sub, now),
            "billing_day": int(sub.get("billing_day") or 1) if sub.get("anchored") else 1, "subscription_id": sub["id"],
            "billing_period": sub.get("period"), **_size_for(sub["plan"]),
        }
    else:
        sub = subs[0]
        values = {
            "plan": "free", "plan_seats": None, "billing_status": "paused" if sub.get("status") == "paused" else "ended",
            "billing_day": 1, "subscription_id": sub["id"], "billing_period": None, **_size_for("free"),
        }
    ws = await workspaces.find_one({"id": workspace_id}, {k: 1 for k in values})
    if ws and all(ws.get(k) == v for k, v in values.items()):
        return
    await workspaces.update_one({"id": workspace_id}, {"$set": {**values, "billing_updated_at": now}})
    from app.billing import credits
    from app.shared import entitlements

    entitlements.forget(workspace_id)
    credits.forget(workspace_id)
    await _audit("workspace_plan_set", sub, by="provider", extra={"values": values, "before": {k: (ws or {}).get(k) for k in values}})
    await _pause_posts(workspace_id, was=(ws or {}).get("billing_status"), now_status=values["billing_status"])
    if values["billing_status"] == "ended" and (ws or {}).get("billing_status") != "ended":
        await _end_bought_credits(workspace_id)


async def _end_bought_credits(workspace_id: str) -> None:
    """Bought credits last while the paid plan runs: when it ends they end too, in every workspace that shared it."""
    from app.billing import credits, topups
    from app.shared.entitlements import group_ids

    try:
        ids = await group_ids(workspace_id)
        for pool_id in await topups.topups_col().distinct("pool_id", {"workspace_id": {"$in": ids}, "status": "paid"}):
            await credits.end_bought(pool_id, why="the paid plan ended")
    except Exception:  # noqa: BLE001 - the plan change itself always goes through
        logger.exception("Couldn't end bought credits for %s", workspace_id)


async def _pause_posts(workspace_id: str, *, was: Any, now_status: str) -> None:
    """Scheduled posts wait while the plan is paused and go back to the queue when it is not. A failure is logged and never stops
    the plan change itself."""
    from app.billing import pause_holds

    try:
        if now_status == "paused" and was != "paused":
            await pause_holds.hold_for_pause(workspace_id)
        elif was == "paused" and now_status != "paused":
            await pause_holds.release_after_pause(workspace_id)
    except Exception:  # noqa: BLE001
        logger.exception("Couldn't %s scheduled posts for the plan pause in %s", "hold" if now_status == "paused" else "release", workspace_id)


# ── webhooks ───────────────────────────────────────────────────────────────

async def receive_webhook(body: bytes, signature: str, event_id: str, provider: Optional[PaymentProvider] = None) -> bool:
    """Check and store one webhook. Returns False when its signature does not match (nothing is stored but the refusal)."""
    provider = provider or get_provider()
    if not provider.verify_webhook(body, signature):
        await _reject("webhook_signature", {"event_id": event_id[:80], "bytes": len(body)})
        return False
    try:
        data = json.loads(body)
    except ValueError:
        await _reject("webhook_unreadable", {"event_id": event_id[:80]})
        return True
    # without the provider's event id, the body itself names the event, the same way in every process
    key = event_id or f"{data.get('event')}:{data.get('created_at')}:{hashlib.sha256(body).hexdigest()[:32]}"
    try:
        await events_col().insert_one({
            "_id": key, "event": data.get("event"), "body": data, "provider_created_at": _ts(data.get("created_at")),
            "received_at": _now(), "processed_at": None, "attempts": 0, "error": None, "outcome": None,
        })
    except DuplicateKeyError:
        return True  # delivered before; already stored
    _spawn(process_event(key))
    return True


_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def process_event(key: str) -> None:
    """Apply one stored webhook. Safe to run twice: it claims the row first, and every step is idempotent."""
    # a row another worker claimed in the last few minutes is still being applied there
    row = await events_col().find_one_and_update(
        {"_id": key, "processed_at": None, "attempts": {"$lt": 8},
         "$or": [{"claimed_at": None}, {"claimed_at": {"$lt": _now() - CLAIM_FOR}}]},
        {"$inc": {"attempts": 1}, "$set": {"claimed_at": _now()}},
    )
    if not row:
        return
    created = row.get("provider_created_at")
    # dates come back from the database without their time zone; they were stored in UTC
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    try:
        outcome = await _apply_event(row["body"], created)
        await events_col().update_one({"_id": key}, {"$set": {"processed_at": _now(), "outcome": outcome, "error": None}})
    except Exception as exc:  # noqa: BLE001 - kept for the retry sweep and the payments watch
        logger.exception("Billing webhook %s failed", key)
        # released at once, so the next sweep retries it
        await events_col().update_one({"_id": key}, {"$set": {"error": str(exc)[:500], "claimed_at": None}})


async def process_pending() -> int:
    """Every minute: apply webhooks not applied yet (a restart between storing and processing, or a failure to retry)."""
    done = 0
    async for row in events_col().find({"processed_at": None, "attempts": {"$lt": 8}}, {"_id": 1}).sort("received_at", 1).limit(200):
        await process_event(row["_id"])
        done += 1
    return done


async def _apply_event(body: dict[str, Any], created_at: Optional[datetime]) -> str:
    event = str(body.get("event") or "")
    payload = body.get("payload") or {}
    sub_entity = (payload.get("subscription") or {}).get("entity") or {}
    pay_entity = (payload.get("payment") or {}).get("entity") or {}
    if event.startswith("subscription."):
        sub = await sync(sub_entity, seen_at=created_at, source=event)
        if sub is None:
            return "unmatched"
        # a charge, or the first payment of a monthly plan taken with its mandate (the days until its billing day)
        if event in ("subscription.charged", "subscription.authenticated") and pay_entity and pay_entity.get("status") == "captured":
            await record_payment(sub, pay_entity, event)
            await invoices.issue_for_payment({**sub}, pay_entity)
        from app.billing import lifecycle

        status = str(sub.get("status"))
        # a change that was approved takes over: the plan it replaces ends at its renewal
        if sub.get("replaces") and status in ("authenticated", "active"):
            await lifecycle.on_change_approved(sub)
        # a renewal above the bank's automatic limit waits for the customer: keep the link to approve it
        if status == "pending" and sub.get("currency") == "INR" and int(sub.get("amount") or 0) > AUTO_DEBIT_LIMIT:
            await lifecycle.open_approval(sub)
        if status == "active":
            await lifecycle.close_approvals(sub["id"])
        return "applied"
    if event in ("refund.processed", "refund.created"):
        refund = (payload.get("refund") or {}).get("entity") or {}
        if event == "refund.processed" or refund.get("status") == "processed":
            await mark_refunded(refund)
        return "applied"
    if event in ("payment.captured", "payment.authorized", "order.paid") and pay_entity.get("order_id"):
        from app.billing import topups

        outcome = await topups.apply_payment(pay_entity)
        if outcome != "unmatched":
            return "applied"
    if event.startswith("payment.dispute."):
        dispute = (payload.get("dispute") or {}).get("entity") or {}
        await record_dispute(dispute, event)
        return "applied" if dispute.get("id") else "ignored"
    if event == "payment.failed" and pay_entity:
        sub_pid = str(pay_entity.get("subscription_id") or "")
        sub = await subs_col().find_one({"provider_subscription_id": sub_pid}, {"_id": 0}) if sub_pid else None
        await record_payment(sub, pay_entity, event)
        return "applied" if sub else "unmatched"
    return "ignored"


async def record_payment(sub: Optional[dict[str, Any]], entity: dict[str, Any], event: str) -> None:
    pid = str(entity.get("id") or "")
    if not pid:
        return
    await payments_col().update_one(
        {"id": pid},
        {"$set": {
            "id": pid, "subscription_id": (sub or {}).get("id"), "workspace_id": (sub or {}).get("workspace_id"),
            "amount": int(entity.get("amount") or 0), "currency": entity.get("currency"), "status": entity.get("status"),
            "method": entity.get("method"), "error": entity.get("error_description"), "error_code": entity.get("error_code"),
            "event": event, "at": _ts(entity.get("created_at")) or _now(), "mode": (sub or {}).get("mode"),
            # what paid it, for the Billing tab and the receipt: never a full card number, only what Razorpay shows anyway
            "card_last4": (entity.get("card") or {}).get("last4"), "card_network": (entity.get("card") or {}).get("network"),
            "vpa": entity.get("vpa"), "bank": entity.get("bank"), "wallet": entity.get("wallet"),
        }},
        upsert=True,
    )


def method_label(payment: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    """How a payment was made, the way the Billing tab and the receipt say it: "Card ending 4242", "UPI Autopay · name@bank"."""
    if not payment:
        return None
    method = str(payment.get("method") or "")
    if method == "card":
        last4 = payment.get("card_last4")
        return {"kind": "card", "label": f"Card ending {last4}" if last4 else "Card"}
    if method == "upi":
        vpa = payment.get("vpa")
        return {"kind": "upi", "label": f"UPI Autopay · {vpa}" if vpa else "UPI Autopay"}
    if method in ("emandate", "netbanking", "nach"):
        bank = payment.get("bank")
        return {"kind": "bank", "label": f"Bank mandate · {bank}" if bank else "Bank mandate"}
    if method:
        return {"kind": method, "label": method.replace("_", " ").capitalize()}
    return None


async def latest_method(workspace_id: str) -> Optional[dict[str, str]]:
    """What paid this workspace's plan most recently."""
    pay = await payments_col().find_one({"workspace_id": workspace_id, "status": "captured"}, {"_id": 0}, sort=[("at", -1)])
    return method_label(pay)


async def mark_refunded(entity: dict[str, Any]) -> None:
    """A refund went through: the payment and its receipt say so. Each refund is kept by its own id, so several partial refunds add up
    and the same refund told twice counts once."""
    pid = str(entity.get("payment_id") or "")
    rid = str(entity.get("id") or "")
    if not pid or not rid:
        return
    await payments_col().update_one({"id": pid}, {"$set": {f"refunds.{rid}": int(entity.get("amount") or 0), "refunded_at": _now()}})
    pay = await payments_col().find_one({"id": pid}, {"amount": 1, "refunds": 1, "topup_id": 1})
    if not pay:
        return
    total = refunded_total(pay)
    status = "refunded" if total >= int(pay.get("amount") or 0) else "partly_refunded"
    await payments_col().update_one({"id": pid}, {"$set": {"refund_status": status, "refunded": total}})
    await get_db()["billing_invoices"].update_one({"payment_id": pid}, {"$set": {"status": status, "refunded": total}})
    if pay.get("topup_id"):
        from app.billing import topups

        await topups.on_refund(pid, total)


def disputes_col():
    return get_db()["billing_disputes"]


# A dispute in one of these states waits on Ekavo's answer or the bank's decision.
OPEN_DISPUTE_STATES = ("open", "under_review", "action_required")


async def record_dispute(entity: dict[str, Any], event: str) -> None:
    """A customer's bank disputed a payment (a chargeback). Kept by its id with its latest state, for the payments watch to raise
    while it is open: an unanswered dispute is lost by default."""
    did = str(entity.get("id") or "")
    if not did:
        return
    pid = str(entity.get("payment_id") or "")
    pay = await payments_col().find_one({"id": pid}, {"workspace_id": 1}) if pid else None
    await disputes_col().update_one(
        {"id": did},
        {"$set": {
            "id": did, "payment_id": pid, "workspace_id": (pay or {}).get("workspace_id"),
            "amount": int(entity.get("amount") or 0), "currency": entity.get("currency") or "INR",
            "status": str(entity.get("status") or event.rsplit(".", 1)[-1]), "reason_code": entity.get("reason_code"),
            "reason": entity.get("reason_description"), "respond_by": _ts(entity.get("respond_by")), "event": event,
            "updated_at": _now(),
        }, "$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )


def refunded_total(payment: dict[str, Any]) -> int:
    """Everything refunded on a payment so far."""
    return sum(int(v or 0) for v in (payment.get("refunds") or {}).values())


# ── records ────────────────────────────────────────────────────────────────

async def _audit(action: str, sub: dict[str, Any], *, by: str, extra: Optional[dict[str, Any]] = None) -> None:
    await get_db()["billing_audit"].insert_one({
        "at": _now(), "by": by, "action": action, "target": sub.get("workspace_id"), "target_name": sub.get("workspace_id"),
        "subscription_id": sub.get("id"), "before": None, "after": {"plan": sub.get("plan"), "status": sub.get("status")},
        "reason": "", **(extra or {}),
    })


async def _reject(kind: str, detail: dict[str, Any]) -> None:
    await rejects_col().insert_one({"kind": kind, "detail": detail, "at": _now()})


async def create_indexes() -> None:
    await subs_col().create_index("id", unique=True)
    await subs_col().create_index("provider_subscription_id")
    await subs_col().create_index([("workspace_id", 1), ("created_at", -1)])
    await subs_col().create_index([("status", 1), ("founding", 1)])
    await events_col().create_index([("processed_at", 1), ("received_at", 1)])
    await events_col().create_index("received_at", expireAfterSeconds=400 * 24 * 3600)
    await payments_col().create_index("id", unique=True)
    await payments_col().create_index([("workspace_id", 1), ("at", -1)])
    await payments_col().create_index([("status", 1), ("at", -1)])
    await rejects_col().create_index("at", expireAfterSeconds=90 * 24 * 3600)
    await disputes_col().create_index("id", unique=True)
    await disputes_col().create_index("status")
    await invoices.create_indexes()
