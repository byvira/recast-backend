"""A paid plan after checkout: changing plan or seats, pausing, cancelling and keeping it, the reminder before each renewal, the grace
period after a failed payment, and approving renewals the bank wants confirmed.

Every change is a new subscription that takes over from the current one, because a provider can edit a running subscription only for
cards, not for UPI Autopay or a bank mandate. The new one starts at the current renewal, so nothing is charged twice:
- a bigger plan or more seats applies at once; the difference for the rest of the period is charged up front;
- a smaller plan, fewer seats, or switching to yearly applies at the renewal; nothing is charged today;
- keeping a plan that was cancelled is the same plan again, from the renewal.
The person approves the new one once in the provider's window, as at checkout. When it is approved, the current one is set to end at
its renewal; until then the workspace keeps what it has.
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from app.billing import plans as P
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.billing.provider import PaymentProvider, ProviderError, get_provider
from app.core.scheduler_lock import distributed_job_lock
from app.db.mongo import get_db, users, workspaces

logger = logging.getLogger(__name__)

REMIND_BEFORE = timedelta(days=3)
# a renewal paused at the end of its period is paused this long before the charge, so the charge never goes out first
PAUSE_LEAD = timedelta(hours=6)
APPROVAL_LINK_DAYS = 7
PAUSE_MONTHS = (1, 2)
PLAN_RANK = {"single": 1, "duo": 2, "large": 3}


def approvals_col():
    return get_db()["billing_approvals"]


def feedback_col():
    return get_db()["billing_feedback"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": "billing_refused", "message": message})


def renewal_at(sub: dict[str, Any]) -> Optional[datetime]:
    """When the current period ends and the next charge falls."""
    for key in ("charge_at", "current_end"):
        when = S._aware(sub.get(key))
        if when:
            return when
    first = sub.get("first_renewal")
    if first:
        d = date.fromisoformat(str(first)[:10])
        return datetime(d.year, d.month, d.day, S.RENEW_HOUR_UTC, tzinfo=timezone.utc)
    return None


def _period_start(sub: dict[str, Any], renews: datetime) -> datetime:
    start = S._aware(sub.get("current_start"))
    if start:
        return start
    if sub.get("period") == "yearly":
        return renews.replace(year=renews.year - 1)
    month = renews.month - 1 or 12
    return renews.replace(year=renews.year - (renews.month == 1), month=month)


async def _owned_live(workspace_id: str) -> dict[str, Any]:
    sub = await S.current_for_workspace(workspace_id)
    if not sub or not S.is_paid(sub):
        raise _refuse(404, "This workspace has no paid plan to change.")
    return sub


# ── changing plan or seats ─────────────────────────────────────────────────

async def quote_change(*, workspace_id: str, plan: str, period: str, seats: int) -> dict[str, Any]:
    """What a change costs today and from the renewal, and when it applies."""
    current = await _owned_live(workspace_id)
    return await _change_terms(current, plan=plan, period=period, seats=seats)


async def _change_terms(current: dict[str, Any], *, plan: str, period: str, seats: int) -> dict[str, Any]:
    from app.shared import entitlements

    if not PR.is_paid(plan) or period not in PR.PERIODS:
        raise _refuse(400, "Choose Single, Duo or Large, monthly or yearly.")
    await PR.load_book()
    seats = P.clamp_seats((await entitlements.load_plans())[plan], seats)
    if plan == current["plan"] and period == current["period"] and seats == int(current.get("seats") or 1):
        raise _refuse(400, "That is the plan you are on.")
    currency = current["currency"]
    # a founding price stays with the workspace while it stays subscribed
    founding = bool(current.get("founding"))
    renew = PR.total(plan, currency, period, seats=seats, founding=founding)  # type: ignore[arg-type]
    renews = renewal_at(current)
    if not renews:
        raise _refuse(409, "This plan's renewal date is not known yet. Try again in a minute.")
    now = _now()
    # a bigger plan, or more seats on the same plan, is wanted now; anything else waits for the renewal. Decided by what is bought,
    # not by the price: on Large the lower seat price from 25 seats makes 26 seats cost less than 24.
    current_seats = int(current.get("seats") or 1)
    moving_up = PLAN_RANK[plan] > PLAN_RANK[current["plan"]] or (plan == current["plan"] and seats > current_seats)
    applies_now = period == current["period"] and moving_up
    today = 0
    difference = renew - int(current.get("amount") or 0)
    if applies_now and difference > 0:
        start = _period_start(current, renews)
        length = max(1.0, (renews - start).total_seconds())
        left = max(0.0, (renews - now).total_seconds())
        today = max(100, round(difference * left / length / 100) * 100)
    return {
        "plan": plan, "period": period, "seats": seats, "currency": currency, "founding": founding,
        "applies": "now" if applies_now else "at_renewal", "today": today, "today_label": PR.format_amount(today, currency),
        "renew": renew, "renew_label": PR.format_amount(renew, currency), "renews_on": renews.date().isoformat(),
        "current": {"plan": current["plan"], "period": current["period"], "seats": current.get("seats"), "amount": current.get("amount")},
    }


async def start_change(
    *, workspace: dict[str, Any], user: dict[str, Any], plan: str, period: str, seats: int,
    keep: bool = False, new_method: bool = False, provider: Optional[PaymentProvider] = None,
) -> dict[str, Any]:
    """Open the subscription that takes over, and return what the browser's checkout needs. `keep` brings back a plan set to end;
    `new_method` is the same plan paid another way (a new card or UPI), from the renewal."""
    provider = provider or get_provider()
    await PR.load_book()
    current = await _owned_live(workspace["id"])
    if keep or new_method:
        if keep and not current.get("cancel_at_period_end"):
            raise _refuse(400, "This plan is not set to end.")
        terms = {
            "plan": current["plan"], "period": current["period"], "seats": int(current.get("seats") or 1), "currency": current["currency"],
            "founding": bool(current.get("founding")), "applies": "at_renewal", "today": 0, "renew": int(current.get("amount") or 0),
        }
        renews = renewal_at(current)
        terms["renews_on"] = renews.date().isoformat() if renews else None
    else:
        terms = await _change_terms(current, plan=plan, period=period, seats=seats)
    # one change at a time: an earlier one that was opened and not approved is let go
    async for stale in S.subs_col().find({"workspace_id": workspace["id"], "replaces": current["id"], "status": "created"}):
        if not await S._let_go(provider, stale):
            raise _refuse(409, "Your earlier change went through. Give it a minute, then check Billing.")

    renews = renewal_at(current)
    unit = PR.unit_amount(terms["plan"], terms["currency"], terms["period"], seats=terms["seats"], founding=terms["founding"])
    quantity = PR.quantity_for(terms["plan"], terms["seats"])
    upfront = None
    if terms["today"] > 0:
        upfront = {"name": f"Ekavo {terms['plan'].title()}, the rest of this period", "amount": terms["today"], "currency": terms["currency"]}
    try:
        plan_id = await S.provider_plan_id(provider, terms["plan"], terms["currency"], terms["period"], unit)
        ref = uuid4().hex
        created = await provider.create_subscription(
            plan_id=plan_id, quantity=quantity, total_count=S.TOTAL_COUNT[terms["period"]],
            notes={"workspace_id": workspace["id"], "sub_ref": ref, "replaces": current["id"]},
            start_at=int(renews.timestamp()) if renews else None, upfront=upfront,
        )
    except ProviderError as exc:
        raise _refuse(502, exc.message) from exc
    now = _now()
    anchored = terms["period"] == "monthly"
    doc = {
        "id": ref, "workspace_id": workspace["id"], "owner_user_id": user["id"], "plan": terms["plan"], "period": terms["period"],
        "currency": terms["currency"], "seats": terms["seats"], "founding": terms["founding"], "unit_amount": unit,
        "amount": unit * quantity, "billing_day": current.get("billing_day") if anchored else None, "anchored": anchored,
        "first_amount": terms["today"], "first_renewal": terms["renews_on"], "replaces": current["id"],
        "starts_now": terms["applies"] == "now", "change_kind": "keep" if keep else "method" if new_method else terms["applies"],
        "provider": provider.name, "mode": provider.mode, "provider_plan_id": plan_id,
        "provider_subscription_id": str(created.get("id")), "status": str(created.get("status") or "created"),
        "cancel_at_period_end": False, "current_start": None, "current_end": None, "charge_at": None, "provider_seen_at": None,
        "customer": current.get("customer") or {}, "created_at": now, "updated_at": now,
    }
    await S.subs_col().insert_one(dict(doc))
    await S._audit("change_opened", doc, by=user["id"], extra={"from": current["id"], "kind": doc["change_kind"]})
    return {
        "subscription": S.public(doc), "terms": terms,
        "checkout": {
            "key": provider.public_key, "subscription_id": doc["provider_subscription_id"], "name": "Ekavo",
            "description": f"{terms['plan'].title()}, {terms['period']}" + (f", {terms['seats']} seats" if terms["plan"] == "large" else ""),
            "prefill": {"name": doc["customer"].get("name", ""), "email": doc["customer"].get("email", "")},
            "notes": {"workspace_id": workspace["id"], "sub_ref": ref}, "mode": provider.mode,
        },
    }


async def on_change_approved(new: dict[str, Any], provider: Optional[PaymentProvider] = None) -> None:
    """The new subscription is approved: the one it replaces ends at its renewal, so the two never charge for the same days."""
    old_id = new.get("replaces")
    if not old_id:
        return
    provider = provider or get_provider()
    # an earlier change waiting for the same renewal is replaced by this one: cancelled now, as nothing was charged on it yet
    async for sibling in S.subs_col().find(
        {"replaces": old_id, "id": {"$ne": new["id"]}, "status": {"$in": ["created", "authenticated"]},
         "created_at": {"$lt": new.get("created_at") or _now()}}, {"_id": 0},
    ):
        if S.not_started(sibling):
            await _cancel_now(provider, sibling)
    old = await S.subs_col().find_one({"id": old_id}, {"_id": 0})
    if not old or old.get("status") in S.ENDED_STATES:
        return
    if not old.get("cancel_at_period_end"):
        try:
            await provider.cancel_subscription(old["provider_subscription_id"], at_cycle_end=True)
        except ProviderError as exc:
            # the payments watch sees two subscriptions in force and raises it
            logger.warning("Could not end %s after its change was approved: %s", old_id, exc.message)
            return
    await S.subs_col().update_one(
        {"id": old_id}, {"$set": {"cancel_at_period_end": True, "replaced_by": new["id"], "pause": None, "updated_at": _now()}},
    )
    await S.apply_for_workspace(new["workspace_id"])


async def _cancel_now(provider: PaymentProvider, sub: dict[str, Any]) -> None:
    try:
        entity = await provider.cancel_subscription(sub["provider_subscription_id"], at_cycle_end=False)
    except ProviderError as exc:
        logger.warning("Could not cancel the replaced change %s: %s", sub["id"], exc.message)
        return
    await S.sync(entity, seen_at=None, source="replaced")


# ── pause, cancel, keep ────────────────────────────────────────────────────

async def request_pause(*, workspace_id: str, user_id: str, months: int) -> dict[str, Any]:
    """Pause from the end of the period already paid for, for one or two months. Nothing is charged meanwhile and nothing is
    deleted; payments start again by themselves."""
    if months not in PAUSE_MONTHS:
        raise _refuse(400, "Pause for 1 or 2 months.")
    sub = await _owned_live(workspace_id)
    if sub.get("cancel_at_period_end"):
        raise _refuse(400, "This plan is already set to end.")
    if await S.scheduled_change(workspace_id, sub["id"]):
        raise _refuse(400, "A plan change starts at the renewal. Pausing would charge for it anyway, so pause after it has started.")
    renews = renewal_at(sub)
    if not renews:
        raise _refuse(409, "This plan's renewal date is not known yet. Try again in a minute.")
    resume_at = _add_months(renews, months)
    pause = {"months": months, "pause_at": renews - PAUSE_LEAD, "resume_at": resume_at, "requested_by": user_id, "requested_at": _now()}
    await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"pause": pause, "updated_at": _now()}})
    await S._audit("pause_requested", sub, by=user_id, extra={"months": months, "resume_at": resume_at})
    return S.public({**sub, "pause": pause}) or {}


async def resume_now(*, workspace_id: str, user_id: str, provider: Optional[PaymentProvider] = None) -> dict[str, Any]:
    provider = provider or get_provider()
    sub = await S.current_for_workspace(workspace_id)
    if not sub or not (sub.get("pause") or sub.get("status") == "paused"):
        raise _refuse(400, "This plan is not paused.")
    if sub.get("status") == "paused":
        try:
            entity = await provider.resume_subscription(sub["provider_subscription_id"])
        except ProviderError as exc:
            raise _refuse(502, exc.message) from exc
        await S.sync(entity, seen_at=None, source="resume")
    await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"pause": None, "updated_at": _now()}})
    await S._audit("resumed", sub, by=user_id)
    await S.apply_for_workspace(workspace_id)
    return S.public(await S.subs_col().find_one({"id": sub["id"]}, {"_id": 0})) or {}


async def record_leaving_reason(*, workspace_id: str, user_id: str, reason: Optional[str], note: Optional[str]) -> None:
    await feedback_col().insert_one({"workspace_id": workspace_id, "user_id": user_id, "reason": reason, "note": (note or "")[:500], "at": _now()})


def _add_months(when: datetime, months: int) -> datetime:
    index = when.year * 12 + when.month - 1 + months
    return when.replace(year=index // 12, month=index % 12 + 1, day=min(when.day, 28))


async def pause_tick(provider: Optional[PaymentProvider] = None) -> int:
    """Pauses that are due go to the provider shortly before the charge; pauses that are over are resumed."""
    provider = provider or get_provider()
    now = _now()
    done = 0
    async for sub in S.subs_col().find({"pause.pause_at": {"$lte": now}, "status": {"$in": ["active", "authenticated"]}}, {"_id": 0}).limit(200):
        try:
            entity = await provider.pause_subscription(sub["provider_subscription_id"])
            await S.sync(entity, seen_at=None, source="pause")
            done += 1
        except ProviderError as exc:
            logger.warning("Pause of %s failed: %s", sub["id"], exc.message)
    async for sub in S.subs_col().find({"pause.resume_at": {"$lte": now}, "status": "paused"}, {"_id": 0}).limit(200):
        try:
            entity = await provider.resume_subscription(sub["provider_subscription_id"])
            await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"pause": None}})
            await S.sync(entity, seen_at=None, source="resume")
            done += 1
        except ProviderError as exc:
            logger.warning("Resume of %s failed: %s", sub["id"], exc.message)
    # a paused plan keeps working until the period it paid for ends; then the workspace moves to its paused state, which no
    # provider message announces
    async for sub in S.subs_col().find({"status": "paused", "current_end": {"$lte": now}}, {"workspace_id": 1}).limit(2000):
        await S.apply_for_workspace(sub["workspace_id"])
    return done


# ── the reminder before a renewal, and the grace period ───────────────────

async def reminder_tick() -> int:
    """Three days before each renewal: a note in Activity and an email to the owner, once per renewal."""
    from app.core.notifications import send_templated_email
    from app.shared.activity.projector import record_system

    now = _now()
    sent = 0
    async for sub in S.subs_col().find(
        {"status": {"$in": ["active", "authenticated"]}, "cancel_at_period_end": {"$ne": True}, "pause": None}, {"_id": 0},
    ).limit(2000):
        renews = renewal_at(sub)
        if not renews or not (now < renews <= now + REMIND_BEFORE) or sub.get("reminded_for") == renews.isoformat():
            continue
        amount = PR.format_amount(int(sub.get("amount") or 0), sub.get("currency") or "INR")
        when = f"{renews.day} {renews.strftime('%b')}"
        try:
            await record_system(
                workspace_id=sub["workspace_id"], key=f"billing_renews:{sub['id']}:{renews.date().isoformat()}", actor_name="Ekavo",
                category="billing", title=f"Your plan renews on {when} for {amount}",
                description="It renews by itself. Change or cancel any time in Billing.", status="info", href="/dashboard/settings?tab=billing",
            )
            owner = await users.find_one({"id": sub.get("owner_user_id")}, {"email": 1, "name": 1})
            if owner and owner.get("email"):
                await send_templated_email("billing-renewal-reminder", owner["email"], {
                    "plan": str(sub.get("plan", "")).title(), "amount": amount, "renews_on": when,
                })
        except Exception as exc:  # noqa: BLE001 - a missed reminder never stops the others
            logger.warning("Renewal reminder for %s failed: %s", sub["id"], exc)
            continue
        await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"reminded_for": renews.isoformat()}})
        sent += 1
    return sent


async def grace_tick() -> int:
    """Workspaces whose seven days of grace ran out move to limited mode; the plan itself is kept, and paying restores everything."""
    now = _now()
    moved = 0
    async for sub in S.subs_col().find({"status": {"$in": ["pending", "halted"]}, "grace_until": {"$lte": now}}, {"workspace_id": 1}).limit(2000):
        before = await workspaces.find_one({"id": sub["workspace_id"]}, {"billing_status": 1})
        await S.apply_for_workspace(sub["workspace_id"])
        after = await workspaces.find_one({"id": sub["workspace_id"]}, {"billing_status": 1})
        if (before or {}).get("billing_status") != (after or {}).get("billing_status"):
            moved += 1
    return moved


# ── approving a renewal the bank wants confirmed ──────────────────────────

async def open_approval(sub: dict[str, Any], provider: Optional[PaymentProvider] = None) -> Optional[dict[str, Any]]:
    """A renewal is waiting on the customer (over the bank's automatic limit): keep the provider's link to approve it, under a token of
    our own for the approval page, the banner and the email. One per renewal."""
    provider = provider or get_provider()
    renews = renewal_at(sub)
    # one link per renewal: named by the period's end, which stays put while the provider's next try moves
    period_end = S._aware(sub.get("current_end")) or renews
    key = f"{sub['id']}:{period_end.date().isoformat() if period_end else 'now'}"
    existing = await approvals_col().find_one({"key": key}, {"_id": 0})
    if existing:
        return existing
    try:
        invoice = await provider.latest_invoice(sub["provider_subscription_id"])
    except ProviderError as exc:
        logger.warning("No invoice for approval of %s: %s", sub["id"], exc.message)
        return None
    url = (invoice or {}).get("short_url")
    if not url:
        return None
    ws = await workspaces.find_one({"id": sub["workspace_id"]}, {"name": 1})
    doc = {
        "key": key, "token": secrets.token_urlsafe(24), "subscription_id": sub["id"], "workspace_id": sub["workspace_id"],
        "workspace_name": (ws or {}).get("name") or "", "plan": sub.get("plan"), "period": sub.get("period"),
        "amount": int((invoice or {}).get("amount") or sub.get("amount") or 0), "currency": sub.get("currency") or "INR",
        "renews_on": renews.date().isoformat() if renews else None, "url": url, "status": "open",
        "created_at": _now(), "expires_at": _now() + timedelta(days=APPROVAL_LINK_DAYS),
    }
    await approvals_col().insert_one(dict(doc))
    return doc


async def approval_by_token(token: str) -> Optional[dict[str, Any]]:
    doc = await approvals_col().find_one({"token": token}, {"_id": 0})
    if not doc:
        return None
    expires = S._aware(doc.get("expires_at"))
    sub = await S.subs_col().find_one({"id": doc["subscription_id"]}, {"_id": 0, "status": 1})
    # paying closes the link, so a closed link on an active plan was approved
    approved = doc.get("status") == "done" or bool(sub and sub.get("status") == "active")
    expired = not approved and (doc.get("status") != "open" or (expires is not None and expires <= _now()))
    return {**doc, "expired": expired, "approved": approved}


async def open_approval_for_workspace(workspace_id: str) -> Optional[dict[str, Any]]:
    return await approvals_col().find_one(
        {"workspace_id": workspace_id, "status": "open", "expires_at": {"$gt": _now()}}, {"_id": 0, "token": 1, "amount": 1, "currency": 1},
        sort=[("created_at", -1)],
    )


async def close_approvals(subscription_id: str) -> None:
    await approvals_col().update_many({"subscription_id": subscription_id, "status": "open"}, {"$set": {"status": "done", "done_at": _now()}})


# ── jobs ───────────────────────────────────────────────────────────────────

@distributed_job_lock("billing_lifecycle", ttl_seconds=900)
async def lifecycle_tick() -> None:
    await pause_tick()
    await grace_tick()


@distributed_job_lock("billing_reminders", ttl_seconds=900)
async def reminders_tick() -> None:
    await reminder_tick()


async def create_indexes() -> None:
    await approvals_col().create_index("token", unique=True)
    await approvals_col().create_index("key", unique=True)
    await approvals_col().create_index([("workspace_id", 1), ("status", 1)])
    await feedback_col().create_index("at")
    await S.subs_col().create_index("replaces")
    await S.subs_col().create_index("pause.pause_at")
    await S.subs_col().create_index("pause.resume_at")
    await S.subs_col().create_index([("status", 1), ("grace_until", 1)])
