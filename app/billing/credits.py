"""Credits: what a workspace may spend on AI and render work each month, and what it has spent.

Every action that makes Ekavo do paid work reserves its credits first, then settles what was really used, or releases the hold when
the work fails or is cancelled. The reserve is one conditional database step ("used + reserved + this must fit"), so two members,
two tabs or two jobs spending at once can never go past the limit together.

Where things live:
- `credit_balances`: one record per pool per month (the plan's allowance, credits granted by staff, used, reserved, the notices
  sent, and what was spent per action and per member). A check is one read.
- `credit_holds`: one record per reservation, keyed by the run and step that made it. A retried job finds its earlier hold and is
  never charged twice. Holds a crash left open are returned by `sweep`.
- `credit_wallets`: credits bought in packs, one record per pool, each pack kept apart with its own end date (12 months, or the
  end of the paid plan). The month's own credits are spent first; when they run short, bought credits are moved into the month as
  needed (`bought_in`), and what was moved but not used goes back to its pack when the next month starts.
- `credit_ledger`: append-only. Every charge, grant, refund, carry-over and movement of bought credits is a new row, never an edit, so every balance can be explained;
  `reconcile` compares the two every night.

The pool is the plan's billing group once plans are on (linked workspaces share one), and the workspace itself before that. The
month is the calendar month in UTC. Before launch (`BILLING_ENFORCED` off) credits are counted and shown but the plan's allowance
never stops anything; a lower limit the owner set for the workspace always applies.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.shared.activity.i18n import phrase, words
from app.billing import costs as C
from app.db.mongo import get_db, workspace_ai_budgets, workspace_members, workspaces

logger = logging.getLogger(__name__)

#: How long a reservation may stay open before the sweep returns it. Long renders pass their own.
HOLD_SECONDS = 2 * 60 * 60
BALANCE_CACHE_SECONDS = 5
LIMIT_CACHE_SECONDS = 30
NOTICE_THRESHOLDS = (100, 80)


def balances_col():
    return get_db()["credit_balances"]


def holds_col():
    return get_db()["credit_holds"]


def ledger_col():
    return get_db()["credit_ledger"]


def wallets_col():
    return get_db()["credit_wallets"]


# Bought credits last this long, or until the paid plan ends.
BOUGHT_MONTHS = 12


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── the month ────────────────────────────────────────────────────────────

def period_of(when: Optional[datetime] = None, billing_day: int = 1) -> str:
    """The credit month `when` falls in. Most pools start a month on the 1st and the month is named "2026-11". A paid plan that renews
    on another day (2 to 28) starts its month on that day, and the month is named by its first day, "2026-11-15", so it never mixes
    with a calendar month."""
    when = when or _now()
    day = billing_day if 1 < billing_day <= 28 else 1
    year, month = when.year, when.month
    if when.day < day:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return f"{year:04d}-{month:02d}" if day == 1 else f"{year:04d}-{month:02d}-{day:02d}"


def resets_at(period: str) -> datetime:
    """The moment this month's credits start again: the same day of the next month (the 1st for a calendar month), midnight UTC."""
    parts = [int(p) for p in period.split("-")]
    year, month = parts[0], parts[1]
    day = parts[2] if len(parts) > 2 else 1
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime(year, month, day, tzinfo=timezone.utc)


def starts_at(period: str) -> datetime:
    """The first moment of a credit month."""
    parts = [int(p) for p in period.split("-")]
    return datetime(parts[0], parts[1], parts[2] if len(parts) > 2 else 1, tzinfo=timezone.utc)


def day_label(when: datetime) -> str:
    """"1 Dec", the way screens and notices say a reset date."""
    return f"{when.day} {when.strftime('%b')}"


# ── the pool ─────────────────────────────────────────────────────────────

@dataclass
class Pool:
    id: str
    workspace_id: str
    enforced: bool
    allowance: Optional[int]
    plan_name: str
    workspace_ids: list[str]
    # the day of the month the pool's credits start again: the paid plan's billing day, or the 1st
    billing_day: int = 1
    # a failed payment's grace period ran out: new AI work waits until it is paid
    limited: bool = False
    # on a yearly paid plan: up to one month of unused credits carries into the next month
    yearly: bool = False

    def period(self, when: Optional[datetime] = None) -> str:
        return period_of(when, self.billing_day)


async def pool_for(workspace_id: str) -> Pool:
    from app.shared.entitlements import entitlements_for

    ent = await entitlements_for(workspace_id)
    pool_id = ent.group_id if ent.enforced else ent.workspace_id
    return Pool(
        id=pool_id, workspace_id=ent.workspace_id, enforced=ent.enforced, allowance=ent.plan_limits.get("credits_month"),
        plan_name=ent.plan_name, workspace_ids=list(ent.scope_ids) if ent.enforced else [ent.workspace_id],
        billing_day=ent.billing_day, limited=ent.billing_status == "limited",
        yearly=ent.enforced and ent.billing_period == "yearly" and ent.plan != "free",
    )


LIMITED_MESSAGE = (
    "Your plan is on hold because the payment didn't go through. You can still edit, schedule and publish what is ready. "
    "New AI work is paused."
)


def _limited_refusal() -> CreditsExhausted:
    return CreditsExhausted({"code": "billing_limited", "limit": "credits_month", "message": LIMITED_MESSAGE, "href": "/dashboard/settings?tab=billing"})


_limit_cache: dict[str, tuple[float, Optional[int]]] = {}


async def owner_limit(pool_id: str) -> Optional[int]:
    """The lower monthly limit an owner set for the pool, or None."""
    hit = _limit_cache.get(pool_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    doc = await workspace_ai_budgets.find_one({"workspace_id": pool_id}, {"monthly_credit_limit": 1}) or {}
    value = doc.get("monthly_credit_limit")
    value = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    _limit_cache[pool_id] = (time.monotonic() + LIMIT_CACHE_SECONDS, value)
    return value


async def set_owner_limit(workspace_id: str, limit: Optional[int]) -> Optional[int]:
    """Set (or clear, with None) the owner's monthly limit. It can never be above the plan's allowance plus credits granted."""
    pool = await pool_for(workspace_id)
    if limit is not None:
        if limit < 0:
            raise HTTPException(status_code=400, detail="A limit cannot be below zero.")
        doc = await balances_col().find_one({"_id": _balance_id(pool.id, pool.period())}, {"granted": 1, "carried": 1}) or {}
        ceiling = (pool.allowance or 0) + _extra(doc) if pool.allowance is not None else None
        if ceiling is not None and limit > ceiling:
            raise HTTPException(status_code=400, detail=f"The limit can be at most {ceiling:,} credits, what the plan gives this month.")
    await workspace_ai_budgets.update_one(
        {"workspace_id": pool.id},
        {"$set": {"monthly_credit_limit": limit, "updated_at": _now()}, "$setOnInsert": {"id": pool.id, "workspace_id": pool.id}},
        upsert=True,
    )
    _limit_cache.pop(pool.id, None)
    forget(pool.id)
    return limit


def effective_limit(*, enforced: bool, allowance: Optional[int], granted: int, cap: Optional[int]) -> Optional[int]:
    """What may be spent this month: the plan's allowance plus credits granted (once plans are on), never above the owner's limit.
    None means nothing limits spending."""
    plan_room = allowance + granted if enforced and allowance is not None else None
    if cap is None:
        return plan_room
    return cap if plan_room is None else min(plan_room, cap)


def _balance_id(pool_id: str, period: str) -> str:
    return f"{pool_id}:{period}"


def _extra(doc: dict) -> int:
    """Credits on top of the plan's allowance this month: those staff granted, those carried over from last month, and bought
    credits moved into it."""
    return int(doc.get("granted") or 0) + int(doc.get("carried") or 0) + int(doc.get("bought_in") or 0)


def previous_period(period: str, billing_day: int = 1) -> str:
    """The credit month just before this one."""
    return period_of(starts_at(period) - timedelta(seconds=1), billing_day)


def carry_amount(prev: Optional[dict], allowance: Optional[int]) -> int:
    """What last month leaves for this one on a yearly plan: what was not used, never more than one month's allowance. Only a month
    that was itself on a yearly plan carries anything."""
    if not prev or not prev.get("yearly") or not allowance or prev.get("allowance") is None:
        return 0
    # bought credits are spent last and go back to their pack, so only the month's own credits count here
    own = int(prev["allowance"]) + int(prev.get("granted") or 0) + int(prev.get("carried") or 0)
    unused = own - int(prev.get("used") or 0)
    return max(0, min(int(allowance), unused))


_ensured: dict[str, tuple[float, Optional[int]]] = {}


async def _ensure_balance(pool: Pool, period: str) -> None:
    """Create this month's record on first use. The allowance only ever goes up within a month, so a plan change never takes away
    credits a member was already given. On a yearly plan the new month starts with last month's unused credits added."""
    bid = _balance_id(pool.id, period)
    hit = _ensured.get(bid)
    if hit and hit[0] > time.monotonic() and hit[1] == pool.allowance:
        return
    update: dict[str, Any] = {
        "$setOnInsert": {
            "pool_id": pool.id, "period": period, "used": 0, "reserved": 0, "granted": 0, "carried": 0, "bought_in": 0, "notices": [],
            "yearly": pool.yearly, "created_at": _now(),
        },
    }
    if pool.allowance is not None:
        update["$max"] = {"allowance": int(pool.allowance)}
    else:
        update["$setOnInsert"]["allowance"] = None
    created = False
    try:
        result = await balances_col().update_one({"_id": bid}, update, upsert=True)
        created = result.upserted_id is not None
    except DuplicateKeyError:  # two first spends at once: the other one made it
        pass
    if created and pool.yearly:
        await _carry_over(pool, period)
    if created:
        await _return_bought(pool.id, period)
    _ensured[bid] = (time.monotonic() + 60, pool.allowance)


async def _carry_over(pool: Pool, period: str) -> None:
    """Add last month's unused credits to this month, once. The ledger row's key is unique to the month, so it can never be added
    twice."""
    try:
        prev = await balances_col().find_one({"_id": _balance_id(pool.id, previous_period(period, pool.billing_day))})
        amount = carry_amount(prev, pool.allowance)
        if amount <= 0:
            return
        await ledger_col().insert_one({
            "_id": uuid4().hex, "key": f"carry:{pool.id}:{period}", "kind": "carry", "pool_id": pool.id, "period": period,
            "workspace_id": pool.workspace_id, "user_id": None, "by": "system", "action": None, "units": None, "credits": amount,
            "note": f"Unused from {prev['period']}", "at": _now(),
        })
        await balances_col().update_one({"_id": _balance_id(pool.id, period)}, {"$inc": {"carried": amount}})
    except DuplicateKeyError:
        pass
    except Exception:  # noqa: BLE001 - a missed carry never stops the month from starting
        logger.exception("Couldn't carry unused credits into %s for pool %s", period, pool.id)


# ── refusing, in plain words ─────────────────────────────────────────────

class CreditsExhausted(HTTPException):
    """Not enough credits for this. Answers 402 with the same `limit_reached` shape as every other plan limit."""

    def __init__(self, detail: dict[str, Any]):
        super().__init__(status_code=402, detail=detail)


def refusal_message(*, need: int, left: int, limit: int, by_owner: bool, period: str) -> str:
    if left > 0:
        if by_owner:
            return f"This needs about {need:,} credits. This workspace has {left:,} left under its monthly limit."
        return f"This needs about {need:,} credits. You have {left:,} left."
    if by_owner:
        return f"This workspace's monthly limit of {limit:,} credits is used. An owner can raise it in Odette under Quotas."
    return f"You've used all {limit:,} credits this month. They start again on {day_label(resets_at(period))}."


def _refusal(pool: Pool, doc: dict, cap: Optional[int], need: int, period: str) -> CreditsExhausted:
    granted = _extra(doc)
    limit = effective_limit(enforced=pool.enforced, allowance=doc.get("allowance"), granted=granted, cap=cap) or 0
    spent = int(doc.get("used") or 0) + int(doc.get("reserved") or 0)
    left = max(0, limit - spent)
    plan_room = (doc.get("allowance") or 0) + granted if pool.enforced and doc.get("allowance") is not None else None
    by_owner = cap is not None and (plan_room is None or cap < plan_room)
    return CreditsExhausted({
        "code": "limit_reached", "limit": "credits_month", "allowed": limit, "used": spent, "adding": need, "left": left,
        "plan": None, "enforced": pool.enforced, "by_owner": by_owner, "resets_at": resets_at(period).isoformat(),
        "message": refusal_message(need=need, left=left, limit=limit, by_owner=by_owner, period=period),
    })


# ── which run a charge belongs to ────────────────────────────────────────

_run: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("_credit_run", default=None)


def bind_run(run_id: Optional[str]) -> None:
    """Charges made from here on in this task belong to this run. A job that is retried or restarted runs its steps again under
    the same run, so each step finds the charge it already made and is not charged twice."""
    _run.set({"id": run_id, "seen": {}} if run_id else None)


_label: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_credit_label", default=None)


def bind_label(label: Optional[str]) -> None:
    """Charges made from here on in this task show under this label in the credits history, in place of their own (a Fresh
    Angles picture shows as one, not as a plain picture). None goes back to each charge's own label."""
    _label.set(label)


def _key_for(step: str) -> tuple[str, Optional[str]]:
    run = _run.get()
    if not run:
        return f"once:{uuid4().hex}:{step}", None
    seen = run["seen"]
    n = seen.get(step, 0)
    seen[step] = n + 1
    return f"{run['id']}:{step}:{n}", run["id"]


# ── reserve, settle, release ─────────────────────────────────────────────

#: Ekavo's own Master Admin works without credits: nothing is reserved, charged or refused for them, in any workspace they open. Looked
#: up from the account (kept for a minute), never taken from the request.
_MASTER_CACHE_SECONDS = 60
_master_admin_cache: dict[str, tuple[float, bool]] = {}


async def is_master_admin(user_id: Optional[str]) -> bool:
    if not user_id:
        return False
    now = time.monotonic()
    hit = _master_admin_cache.get(user_id)
    if hit and hit[0] > now:
        return hit[1]
    doc = await get_db()["users"].find_one({"id": user_id}, {"is_master_admin": 1})
    value = bool((doc or {}).get("is_master_admin"))
    _master_admin_cache[user_id] = (now + _MASTER_CACHE_SECONDS, value)
    return value


@dataclass
class Hold:
    action: str
    credits: int
    units: float
    workspace_id: str
    user_id: Optional[str]
    pool_id: Optional[str] = None
    period: Optional[str] = None
    key: Optional[str] = None
    run_id: Optional[str] = None
    duplicate: bool = False
    done: bool = False
    #: set by the caller when the real amount differs from the estimate (minutes really narrated, for example)
    actual_units: Optional[float] = None
    #: set by the caller when nothing was made after all (no picture came back), so nothing is charged
    nothing_made: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def free(self) -> bool:
        return self.credits <= 0 and not self.key


async def reserve(
    workspace_id: str, user_id: Optional[str], action: str, units: float = 1, *, step: Optional[str] = None,
    hold_seconds: int = HOLD_SECONDS, label: Optional[str] = None,
) -> Hold:
    """Set aside the credits for one action before the work starts. Raises `CreditsExhausted` (402) when they do not fit."""
    table = await C.costs()
    if action not in table:
        raise KeyError(f"Unknown credit action: {action}")
    label = _label.get() or label
    need = C.price_with(table, action, units)
    hold = Hold(action=action, credits=need, units=float(units or 0), workspace_id=workspace_id, user_id=user_id)
    if need <= 0 or not workspace_id or await is_master_admin(user_id):
        hold.credits = 0
        return hold

    pool = await pool_for(workspace_id)
    if pool.limited:
        raise _limited_refusal()
    period = pool.period()
    await _ensure_balance(pool, period)
    key, run_id = _key_for(step or action)
    hold.pool_id, hold.period, hold.key, hold.run_id = pool.id, period, key, run_id
    hold_id = f"{pool.id}:{key}"
    # A hold starts as "pending" and becomes "open" only once its credits are really set aside, so a crash between the two steps
    # never makes the sweep give back credits that were not taken.
    fresh = {
        "pool_id": pool.id, "period": period, "workspace_id": workspace_id, "user_id": user_id, "action": action, "units": hold.units,
        "credits": need, "run_id": run_id, "status": "pending", "label": label, "created_at": _now(),
        "expires_at": _now() + timedelta(seconds=hold_seconds),
    }
    try:
        await holds_col().insert_one({"_id": hold_id, **fresh})
    except DuplicateKeyError:
        # This run already took this step. If that step was charged (or is still running), nothing more is taken. If it ended
        # without being charged (it failed, was cancelled, or a crash left it unfinished), the retry takes the hold over and pays.
        retaken = await holds_col().find_one_and_update(
            {"_id": hold_id, "$or": [
                {"status": {"$in": ["released", "abandoned"]}},
                {"status": "pending", "expires_at": {"$lt": _now()}},
            ]},
            {"$set": fresh},
        )
        if not retaken:
            hold.duplicate = True
            return hold

    cap = await owner_limit(pool.id)
    bid = _balance_id(pool.id, period)
    match: dict[str, Any] = {"_id": bid}
    limit_expr: Any = None
    if pool.enforced:
        limit_expr = {"$add": [{"$ifNull": ["$allowance", 0]}, "$granted", {"$ifNull": ["$carried", 0]}, {"$ifNull": ["$bought_in", 0]}]}
    if cap is not None:
        limit_expr = cap if limit_expr is None else {"$min": [limit_expr, cap]}
    if limit_expr is not None:
        match["$expr"] = {"$lte": [{"$add": ["$used", "$reserved", need]}, limit_expr]}
    result = await balances_col().update_one(match, {"$inc": {"reserved": need}})
    if result.matched_count == 0 and pool.enforced and await _draw_for(pool, period, need, cap):
        # the month's own credits ran short: bought credits were moved in, so try once more
        result = await balances_col().update_one(match, {"$inc": {"reserved": need}})
    if result.matched_count == 0:
        await holds_col().update_one({"_id": hold_id, "status": "pending"}, {"$set": {"status": "released", "released_at": _now()}})
        doc = await balances_col().find_one({"_id": bid}) or {}
        raise _refusal(pool, doc, cap, need, period)
    await holds_col().update_one({"_id": hold_id, "status": "pending"}, {"$set": {"status": "open"}})
    forget(pool.id)
    return hold


async def settle(hold: Hold, units: Optional[float] = None) -> int:
    """Charge what the work really used (the estimate unless `units` says otherwise). Safe to call twice."""
    if hold.done or hold.duplicate or not hold.key:
        hold.done = True
        return 0
    hold.done = True
    actual_units = hold.units if units is None else float(units)
    credits = hold.credits if units is None else C.price_with(await C.costs(), hold.action, actual_units)
    before = await holds_col().find_one_and_update(
        {"_id": f"{hold.pool_id}:{hold.key}", "status": {"$in": ["open", "expired"]}},
        {"$set": {"status": "settled", "settled_at": _now(), "charged": credits, "units": actual_units}},
    )
    if not before:
        return 0
    inc: dict[str, Any] = {"used": credits}
    if before.get("status") == "open":
        inc["reserved"] = -int(before.get("credits") or 0)
    if credits:
        inc[f"by_action.{hold.action}"] = credits
        inc[f"by_member.{hold.user_id or 'system'}"] = credits
    bid = _balance_id(hold.pool_id or "", hold.period or "")
    await balances_col().update_one({"_id": bid}, {"$inc": inc})
    if credits:
        try:
            await ledger_col().insert_one({
                "_id": uuid4().hex, "key": hold.key, "kind": "charge", "pool_id": hold.pool_id, "period": hold.period,
                "workspace_id": hold.workspace_id, "user_id": hold.user_id, "action": hold.action, "units": actual_units,
                "credits": credits, "run_id": hold.run_id, "label": before.get("label"), "at": _now(),
            })
        except DuplicateKeyError:
            pass
    forget(hold.pool_id)
    if credits:
        _spawn(_maybe_notify(hold.pool_id or "", hold.period or "", hold.workspace_id))
    return credits


async def release(hold: Hold) -> None:
    """Give the held credits back: the work failed, was cancelled, or made nothing. Safe to call twice."""
    if hold.done or hold.duplicate or not hold.key:
        hold.done = True
        return
    hold.done = True
    before = await holds_col().find_one_and_update(
        {"_id": f"{hold.pool_id}:{hold.key}", "status": "open"},
        {"$set": {"status": "released", "released_at": _now()}},
    )
    if before:
        await balances_col().update_one(
            {"_id": _balance_id(hold.pool_id or "", hold.period or "")}, {"$inc": {"reserved": -int(before.get("credits") or 0)}},
        )
        forget(hold.pool_id)


@asynccontextmanager
async def spend(
    workspace_id: str, user_id: Optional[str], action: str, units: float = 1, *, step: Optional[str] = None,
    hold_seconds: int = HOLD_SECONDS, label: Optional[str] = None,
) -> AsyncIterator[Hold]:
    """Reserve, run the work, then settle; release instead when the work raises, is cancelled or sets `nothing_made`.

        async with credits.spend(ws, user, "picture", step=f"picture:{n}") as hold:
            asset = await make_picture()
            if asset is None:
                hold.nothing_made = True
    """
    hold = await reserve(workspace_id, user_id, action, units, step=step, hold_seconds=hold_seconds, label=label)
    try:
        yield hold
    except BaseException:
        await asyncio.shield(release(hold))
        raise
    if hold.nothing_made:
        await release(hold)
    else:
        await settle(hold, hold.actual_units)


async def charge(
    workspace_id: str, user_id: Optional[str], action: str, units: float = 1, *, step: Optional[str] = None,
    label: Optional[str] = None,
) -> Hold:
    """Reserve and settle at once, for work that already happened and cannot fail any more."""
    hold = await reserve(workspace_id, user_id, action, units, step=step, label=label)
    await settle(hold)
    return hold


async def require_room(workspace_id: str, credits: int = 1, user_id: Optional[str] = None) -> None:
    """Refuse (402) when this many credits would not fit, without setting anything aside. For a quick check before a long run.

    Credits set aside by work still running are not counted here: the caller may be that very work (a run checks again after
    reserving its own steps), and the reserve itself is what stops two spends from going past the limit together. A Master Admin is never refused."""
    if credits <= 0 or not workspace_id or await is_master_admin(user_id):
        return
    pool = await pool_for(workspace_id)
    if pool.limited:
        raise _limited_refusal()
    cap = await owner_limit(pool.id)
    if not pool.enforced and cap is None:
        return
    period = pool.period()
    await _ensure_balance(pool, period)
    doc = await balances_col().find_one({"_id": _balance_id(pool.id, period)}) or {}
    wallet = (await bought_left(pool.id))[0] if pool.enforced else 0
    limit = effective_limit(enforced=pool.enforced, allowance=doc.get("allowance"), granted=_extra(doc) + wallet, cap=cap)
    if limit is None:
        return
    if int(doc.get("used") or 0) + credits > limit:
        raise _refusal(pool, doc, cap, credits, period)


async def has_room(workspace_id: str, credits: int = 1, user_id: Optional[str] = None) -> bool:
    try:
        await require_room(workspace_id, credits, user_id)
    except CreditsExhausted:
        return False
    return True


def is_out_of_credits(exc: BaseException) -> bool:
    return isinstance(exc, CreditsExhausted) or (
        isinstance(exc, HTTPException) and isinstance(exc.detail, dict) and exc.detail.get("limit") == "credits_month"
    )


# ── bought credits ───────────────────────────────────────────────────────

def _ledger_row(kind: str, pool_id: str, period: str, credits: int, *, key: Optional[str] = None, workspace_id: Optional[str] = None,
                note: Optional[str] = None, by: str = "system") -> dict[str, Any]:
    return {
        "_id": uuid4().hex, "key": key or f"{kind}:{uuid4().hex}", "kind": kind, "pool_id": pool_id, "period": period,
        "workspace_id": workspace_id, "user_id": None, "by": by, "action": None, "units": None, "credits": int(credits),
        "note": note, "at": _now(),
    }


async def add_bought(workspace_id: str, *, topup_id: str, credits: int, label: str) -> bool:
    """Put a bought pack's credits in the pool's wallet. Once per purchase: telling it twice adds nothing. Returns True when added."""
    pool = await pool_for(workspace_id)
    now = _now()
    lot = {"id": topup_id, "credits": int(credits), "left": int(credits), "bought_at": now,
           "expires_at": _add_months(now, BOUGHT_MONTHS), "label": label}
    try:
        await ledger_col().insert_one(_ledger_row("purchase", pool.id, pool.period(), credits, key=f"purchase:{topup_id}",
                                                  workspace_id=workspace_id, note=label))
    except DuplicateKeyError:
        return False
    await wallets_col().update_one(
        {"_id": pool.id, "lots.id": {"$ne": topup_id}}, {"$push": {"lots": lot}, "$set": {"pool_id": pool.id}}, upsert=True,
    )
    forget(pool.id)
    return True


def _add_months(when: datetime, months: int) -> datetime:
    index = when.year * 12 + when.month - 1 + months
    return when.replace(year=index // 12, month=index % 12 + 1, day=min(when.day, 28))


def _live_lots(wallet: Optional[dict], now: datetime) -> list[dict]:
    lots = [lot for lot in (wallet or {}).get("lots") or [] if int(lot.get("left") or 0) > 0]
    live = [lot for lot in lots if (lot["expires_at"] if lot["expires_at"].tzinfo else lot["expires_at"].replace(tzinfo=timezone.utc)) > now]
    return sorted(live, key=lambda lot: lot["expires_at"])


async def bought_left(pool_id: str) -> tuple[int, Optional[datetime]]:
    """Bought credits not used yet, and when the first of them ends."""
    lots = _live_lots(await wallets_col().find_one({"_id": pool_id}), _now())
    first = lots[0]["expires_at"] if lots else None
    if first is not None and first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return sum(int(lot["left"]) for lot in lots), first


async def _draw_for(pool: Pool, period: str, need: int, cap: Optional[int]) -> bool:
    """Move just enough bought credits into this month for `need` more, the packs ending soonest first. The owner's own limit is
    never passed by buying credits. Returns True when enough was moved."""
    bid = _balance_id(pool.id, period)
    doc = await balances_col().find_one({"_id": bid}) or {}
    if doc.get("allowance") is None:
        return False
    wanted = int(doc.get("used") or 0) + int(doc.get("reserved") or 0) + need
    if cap is not None and wanted > cap:
        return False
    short = wanted - (int(doc["allowance"]) + _extra(doc))
    if short <= 0:
        return True
    now = _now()
    moved = 0
    for lot in _live_lots(await wallets_col().find_one({"_id": pool.id}), now):
        take = min(int(lot["left"]), short - moved)
        res = await wallets_col().update_one(
            {"_id": pool.id, "lots": {"$elemMatch": {"id": lot["id"], "left": {"$gte": take}, "expires_at": {"$gt": now}}}},
            {"$inc": {"lots.$.left": -take}},
        )
        if not res.modified_count:
            continue  # another spend took it first; the next pack is tried
        await balances_col().update_one({"_id": bid}, {"$inc": {"bought_in": take, f"bought_lots.{lot['id']}": take}})
        await ledger_col().insert_one(_ledger_row("bought_in", pool.id, period, take, workspace_id=pool.workspace_id, note=lot["id"]))
        moved += take
        if moved >= short:
            break
    if moved:
        forget(pool.id)
    return moved >= short


async def _return_bought(pool_id: str, period: str) -> None:
    """A new month started: bought credits moved into an earlier month and not used there go back to their packs. Each month is
    settled once."""
    try:
        async for doc in balances_col().find({"pool_id": pool_id, "period": {"$ne": period}, "bought_in": {"$gt": 0},
                                              "bought_returned": {"$ne": True}}):
            claimed = await balances_col().find_one_and_update(
                {"_id": doc["_id"], "bought_returned": {"$ne": True}}, {"$set": {"bought_returned": True}},
            )
            if not claimed:
                continue
            room = int(doc.get("allowance") or 0) + _extra(doc) - int(doc.get("used") or 0) - int(doc.get("reserved") or 0)
            back = max(0, min(int(doc.get("bought_in") or 0), room))
            # given back to the packs it came from, the one ending last first
            lots = await wallets_col().find_one({"_id": pool_id}) or {}
            ends = {lot["id"]: lot["expires_at"] for lot in lots.get("lots") or []}
            for lot_id, n in sorted((doc.get("bought_lots") or {}).items(), key=lambda kv: ends.get(kv[0]) or _now(), reverse=True):
                give = min(int(n), back)
                if give <= 0:
                    break
                await wallets_col().update_one({"_id": pool_id, "lots.id": lot_id}, {"$inc": {"lots.$.left": give}})
                await balances_col().update_one({"_id": doc["_id"]}, {"$inc": {"bought_in": -give, f"bought_lots.{lot_id}": -give}})
                await ledger_col().insert_one(_ledger_row("bought_back", pool_id, doc["period"], give, note=lot_id))
                back -= give
        forget(pool_id)
    except Exception:  # noqa: BLE001 - a month always starts; a missed return shows in the nightly check
        logger.exception("Couldn't return unused bought credits for pool %s", pool_id)


async def end_bought(
    pool_id: str, *, why: str, lot_id: Optional[str] = None, limit: Optional[int] = None, kind: str = "expire",
) -> int:
    """End bought credits: every pack (the plan ended), one pack (its 12 months passed), or one pack up to `limit` (refunded, kind
    "clawback"). Returns how many ended."""
    wallet = await wallets_col().find_one({"_id": pool_id})
    ended = 0
    period = period_of()
    for lot in (wallet or {}).get("lots") or []:
        if lot_id and lot["id"] != lot_id:
            continue
        take = int(lot.get("left") or 0) if limit is None else min(int(lot.get("left") or 0), max(0, limit - ended))
        if take <= 0:
            continue
        res = await wallets_col().update_one(
            {"_id": pool_id, "lots": {"$elemMatch": {"id": lot["id"], "left": {"$gte": take}}}}, {"$inc": {"lots.$.left": -take}},
        )
        if res.modified_count:
            await ledger_col().insert_one(_ledger_row(kind, pool_id, period, take, note=f"{lot['id']}: {why}"))
            ended += take
    forget(pool_id)
    return ended


async def expire_bought() -> int:
    """Nightly: packs past their end date end."""
    now = _now()
    ended = 0
    async for wallet in wallets_col().find({"lots": {"$elemMatch": {"expires_at": {"$lte": now}, "left": {"$gt": 0}}}}):
        for lot in wallet.get("lots") or []:
            when = lot["expires_at"] if lot["expires_at"].tzinfo else lot["expires_at"].replace(tzinfo=timezone.utc)
            if when <= now and int(lot.get("left") or 0) > 0:
                ended += await end_bought(wallet["_id"], why="12 months passed", lot_id=lot["id"])
    return ended


# ── what screens show ────────────────────────────────────────────────────

_balance_cache: dict[str, tuple[float, dict]] = {}


def forget(pool_id: Optional[str] = None) -> None:
    if pool_id is None:
        _balance_cache.clear()
    else:
        _balance_cache.pop(pool_id, None)


async def _member_names(user_ids: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    ids = [u for u in user_ids if u and u != "system"]
    if not ids:
        return names
    async for u in get_db()["users"].find({"id": {"$in": ids}}, {"id": 1, "name": 1, "full_name": 1, "email": 1}):
        names[u["id"]] = u.get("full_name") or u.get("name") or (u.get("email") or "").split("@")[0] or "A member"
    return names


async def summary(workspace_id: str) -> dict[str, Any]:
    """The pool's month at a glance: allowance, used, left, the reset date, and where the credits went by area, action and
    member. Every member may see it."""
    pool = await pool_for(workspace_id)
    hit = _balance_cache.get(pool.id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    period = pool.period()
    await _ensure_balance(pool, period)
    doc = await balances_col().find_one({"_id": _balance_id(pool.id, period)}) or {}
    cap = await owner_limit(pool.id)
    table = await C.costs()
    granted = int(doc.get("granted") or 0)
    carried = int(doc.get("carried") or 0)
    bought_in = int(doc.get("bought_in") or 0)
    bought, bought_until = await bought_left(pool.id)
    used = int(doc.get("used") or 0)
    reserved = int(doc.get("reserved") or 0)
    allowance = doc.get("allowance")
    plan_total = allowance + granted + carried + bought_in if allowance is not None else None
    limit = effective_limit(enforced=pool.enforced, allowance=allowance, granted=granted + carried + bought_in, cap=cap)
    # what the meter is drawn against: the enforced limit, or before launch the plan's own number, so the bar means something
    shown_total = limit if limit is not None else plan_total
    left = max(0, shown_total - used - reserved) if shown_total is not None else None
    if shown_total is None:
        percent = None
    elif shown_total <= 0:
        percent = 100 if limit is not None else 0       # a limit of 0 means nothing may be spent
    else:
        percent = min(100, round(100 * (used + reserved) / shown_total))

    by_action = {k: int(v) for k, v in (doc.get("by_action") or {}).items() if v}
    modules: dict[str, int] = {}
    for key, n in by_action.items():
        module = table.get(key, {}).get("module", "system")
        modules[module] = modules.get(module, 0) + n
    by_member = {k: int(v) for k, v in (doc.get("by_member") or {}).items() if v}
    names = await _member_names(list(by_member))
    out = {
        "workspace_id": pool.workspace_id, "pool_id": pool.id, "shared": len(pool.workspace_ids) > 1,
        "period": period, "resets_at": resets_at(period).isoformat(), "resets_on": day_label(resets_at(period)),
        "enforced": pool.enforced, "plan_name": pool.plan_name,
        "allowance": allowance, "granted": granted, "carried": carried, "plan_total": plan_total,
        "bought_left": bought, "bought_until": bought_until.isoformat() if bought_until else None, "bought_used": bought_in, "owner_limit": cap, "limit": limit,
        "used": used, "reserved": reserved, "left": left, "percent": percent,
        "state": None if percent is None else ("over" if percent >= 100 else "near" if percent >= 80 else "ok"),
        "modules": [
            {"key": m, "label": C.MODULES[m], "credits": modules.get(m, 0), "share": round(100 * modules.get(m, 0) / used) if used else 0}
            for m in C.MODULES if m != "system"
        ],
        "actions": sorted(
            ({"id": k, "label": table.get(k, {}).get("label", k), "module": table.get(k, {}).get("module", "system"), "credits": n}
             for k, n in by_action.items()),
            key=lambda r: -r["credits"],
        ),
        "members": sorted(
            ({"user_id": k, "name": names.get(k, "Automations" if k == "system" else "A former member"), "credits": n}
             for k, n in by_member.items()),
            key=lambda r: -r["credits"],
        ),
    }
    out["insight"] = insight(out)
    _balance_cache[pool.id] = (time.monotonic() + BALANCE_CACHE_SECONDS, out)
    return out


def insight(s: dict[str, Any]) -> dict[str, str]:
    """One true sentence about this month's credits, most useful first: used up, nearly used up, on pace to run out, where most
    went, or how to start. `kind` lets the screen pick the colour."""
    used, total, left = s["used"] + s["reserved"], s["limit"] if s["limit"] is not None else s["plan_total"], s["left"]
    reset = s["resets_on"]
    blocking = s["limit"] is not None
    if total is not None and used >= total and (total > 0 or blocking):
        if blocking:
            return {"kind": "warn", "text": f"This month's credits are used. New AI work waits until {reset}. Editing, scheduling and publishing keep working."}
        return {"kind": "warn", "text": f"This month's {total:,} credits are used. Nothing stops yet while plans are being set up."}
    if total and used * 100 >= total * 80:
        return {"kind": "warn", "text": f"{left:,} credits left until {reset}."}
    now = _now()
    # days into this credit month and its length, counted from its own first day (the billing day on some paid plans)
    start = starts_at(s["period"])
    day = max(1, (now - start).days + 1)
    days_in_month = (resets_at(s["period"]) - start).days
    if total and used > 0:
        projected = used / day * days_in_month
        if projected > total:
            run_out = min(days_in_month, max(day, int(total / (used / day))))
            return {"kind": "warn", "text": f"At this pace the credits run out around day {run_out} of the month. They start again on {reset}."}
    top = max(s["modules"], key=lambda m: m["credits"], default=None)
    if not used or not top or not top["credits"]:
        return {"kind": "empty", "text": "No credits used yet this month. Make a post, a picture, a recording or a video and it shows up here."}
    return {"kind": "info", "text": f"{top['label']} took most of your credits this month ({top['share']}%)."}


async def ledger(
    workspace_id: str, *, period: Optional[str] = None, limit: int = 50, before: Optional[str] = None, for_ops: bool = False,
) -> dict[str, Any]:
    """The pool's history for one month, newest first, with plain labels and names. `before` is the `at` of the last row seen.
    Staff notes on grants and refunds are only shown in Ops."""
    pool = await pool_for(workspace_id)
    period = period or pool.period()
    query: dict[str, Any] = {"pool_id": pool.id, "period": period}
    if not for_ops:
        # moving bought credits into a month and back is bookkeeping; the purchase itself is what members see
        query["kind"] = {"$nin": list(INTERNAL_KINDS)}
    if before:
        try:
            query["at"] = {"$lt": datetime.fromisoformat(before)}
        except ValueError:
            raise HTTPException(status_code=400, detail="That page marker is not a date.")
    rows = await ledger_col().find(query).sort("at", -1).limit(max(1, min(limit, 200)) + 1).to_list(None)
    more = len(rows) > limit
    rows = rows[:limit]
    table = await C.costs()
    names = await _member_names(list({r.get("user_id") for r in rows if r.get("user_id")}))
    ws_names: dict[str, str] = {}
    if len(pool.workspace_ids) > 1:
        async for w in workspaces.find({"id": {"$in": pool.workspace_ids}}, {"id": 1, "name": 1}):
            ws_names[w["id"]] = w.get("name") or ""
    items = []
    for r in rows:
        at = r["at"] if r["at"].tzinfo else r["at"].replace(tzinfo=timezone.utc)
        items.append({
            "id": r["_id"], "kind": r["kind"], "action": r.get("action"),
            "label": r.get("label") or table.get(r.get("action") or "", {}).get("label") or LEDGER_LABELS.get(r["kind"], r["kind"]),
            "credits": int(r.get("credits") or 0), "units": r.get("units"), "unit_label": C.unit_label(table.get(r.get("action") or "", {})),
            "member": names.get(r.get("user_id") or "", "Automations" if not r.get("user_id") else "A former member") if r["kind"] == "charge" else None,
            "workspace": ws_names.get(r.get("workspace_id") or "") or None, "note": r.get("note") if for_ops else None, "at": at.isoformat(),
        })
    return {"period": period, "items": items, "next": items[-1]["at"] if more and items else None}


LEDGER_LABELS = {
    "grant": "Credits added", "refund": "Credits returned", "carry": "Unused credits carried over", "purchase": "Credits bought",
    "bought_in": "Bought credits used this month", "bought_back": "Unused bought credits kept for later",
    "expire": "Bought credits ended", "clawback": "Bought credits refunded",
}
INTERNAL_KINDS = ("bought_in", "bought_back")


# ── staff changes ────────────────────────────────────────────────────────

async def grant(workspace_id: str, credits: int, *, by: str, note: str) -> dict[str, Any]:
    """Add credits to this month's pool (staff, from Ops)."""
    pool = await pool_for(workspace_id)
    period = pool.period()
    await _ensure_balance(pool, period)
    await balances_col().update_one({"_id": _balance_id(pool.id, period)}, {"$inc": {"granted": int(credits)}})
    await ledger_col().insert_one({
        "_id": uuid4().hex, "key": f"grant:{uuid4().hex}", "kind": "grant", "pool_id": pool.id, "period": period,
        "workspace_id": workspace_id, "user_id": None, "by": by, "action": None, "units": None, "credits": int(credits),
        "note": note, "at": _now(),
    })
    forget(pool.id)
    return await summary(workspace_id)


async def refund(workspace_id: str, credits: int, *, by: str, note: str) -> dict[str, Any]:
    """Return credits already charged this month (staff, from Ops), never more than were used."""
    pool = await pool_for(workspace_id)
    period = pool.period()
    await _ensure_balance(pool, period)
    bid = _balance_id(pool.id, period)
    result = await balances_col().update_one({"_id": bid, "used": {"$gte": int(credits)}}, {"$inc": {"used": -int(credits)}})
    if result.matched_count == 0:
        raise HTTPException(status_code=400, detail="That is more than this workspace has used this month.")
    await ledger_col().insert_one({
        "_id": uuid4().hex, "key": f"refund:{uuid4().hex}", "kind": "refund", "pool_id": pool.id, "period": period,
        "workspace_id": workspace_id, "user_id": None, "by": by, "action": None, "units": None, "credits": int(credits),
        "note": note, "at": _now(),
    })
    forget(pool.id)
    return await summary(workspace_id)


# ── notices at 80% and 100% ──────────────────────────────────────────────

_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _maybe_notify(pool_id: str, period: str, workspace_id: str) -> None:
    """Tell every member once a month when 80%, and again when all, of the credits that really limit them are used. Before launch
    only the owner's own limit limits anything, so only that one is told."""
    try:
        doc = await balances_col().find_one({"_id": _balance_id(pool_id, period)})
        if not doc:
            return
        pool = await pool_for(workspace_id)
        cap = await owner_limit(pool_id)
        limit = effective_limit(enforced=pool.enforced, allowance=doc.get("allowance"), granted=_extra(doc), cap=cap)
        if not limit:
            return
        used = int(doc.get("used") or 0)
        for threshold in NOTICE_THRESHOLDS:
            if used * 100 < limit * threshold:
                continue
            marks = [t for t in NOTICE_THRESHOLDS if t <= threshold]
            claimed = await balances_col().update_one(
                {"_id": doc["_id"], "notices": {"$ne": threshold}}, {"$addToSet": {"notices": {"$each": marks}}},
            )
            if claimed.modified_count:
                await _send_notice(pool, period, threshold, used=used, limit=limit, by_owner=cap is not None and cap == limit)
            return
    except Exception as exc:  # noqa: BLE001 - a notice must never break a charge
        logger.warning("Credit notice for %s failed: %s", pool_id, exc)


def notice_words(threshold: int, *, used: int, limit: int, period: str, by_owner: bool) -> tuple[str, str]:
    reset = day_label(resets_at(period))
    if threshold >= 100:
        title = "This month's credits are used up"
        body = (
            f"All {limit:,} credits under this workspace's monthly limit are used. An owner can raise the limit in Odette under Quotas."
            if by_owner else
            f"All {limit:,} credits this month are used. New AI work and renders wait until {reset}. Editing, scheduling and "
            "publishing what is ready keep working."
        )
    else:
        title = "80% of this month's credits are used"
        body = f"{used:,} of {limit:,} credits are used. They start again on {reset}."
    return title, body


async def _send_notice(pool: Pool, period: str, threshold: int, *, used: int, limit: int, by_owner: bool) -> None:
    from app.core.notifications import send_templated_email
    from app.shared.activity.projector import record_system

    title, body = notice_words(threshold, used=used, limit=limit, period=period, by_owner=by_owner)
    for ws_id in pool.workspace_ids:
        await record_system(
            workspace_id=ws_id, key=f"credits:{ws_id}:{period}:{threshold}", actor_name="Ekavo", category="credits",
            title=title, description=body, status="failed" if threshold >= 100 else "warning", href="/dashboard/settings?tab=billing",
            metadata={"used": used, "limit": limit, "threshold": threshold, "period": period},
        )
    emails: set[str] = set()
    member_ids = await workspace_members.distinct("user_id", {"workspace_id": {"$in": pool.workspace_ids}, "status": "active"})
    async for u in get_db()["users"].find({"id": {"$in": member_ids}}, {"email": 1}):
        if u.get("email"):
            emails.add(str(u["email"]).lower())
    ws = await workspaces.find_one({"id": pool.id}, {"name": 1}) or {}
    template = "credits-used-up" if threshold >= 100 else "credits-running-low"
    variables = {
        "workspace_name": ws.get("name") or "your workspace", "used": f"{used:,}", "limit": f"{limit:,}",
        "resets_on": day_label(resets_at(period)), "title": title, "message": body,
    }
    for email in sorted(emails):
        await send_templated_email(template, email, variables)


async def pause_notice(workspace_id: str, what: str, *, href: Optional[str] = None, key: Optional[str] = None) -> None:
    """Background work that stopped because credits ran out says so in Activity, once per piece of work and month."""
    from app.shared.activity.projector import record_system

    period = (await pool_for(workspace_id)).period()
    try:
        await record_system(
            workspace_id=workspace_id, key=f"credits_paused:{workspace_id}:{period}:{key or what}", actor_name="Ekavo", category="credits",
            title=phrase("{what} paused: credits are used up", what=words(what)),
            description=phrase("It carries on by itself when credits start again on {day}, or sooner if more are added. Posts already made still publish.",
                               day=day_label(resets_at(period))),
            status="warning", href=href or "/dashboard/settings?tab=billing",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Credit pause notice for %s failed: %s", workspace_id, exc)


# ── housekeeping ─────────────────────────────────────────────────────────

async def sweep() -> int:
    """Return holds a crash or restart left open past their time. The work they were for is treated as not done."""
    returned = 0
    async for hold in holds_col().find({"status": "open", "expires_at": {"$lt": _now()}}).limit(500):
        before = await holds_col().find_one_and_update(
            {"_id": hold["_id"], "status": "open"}, {"$set": {"status": "expired", "released_at": _now()}},
        )
        if before:
            await balances_col().update_one(
                {"_id": _balance_id(before["pool_id"], before["period"])}, {"$inc": {"reserved": -int(before.get("credits") or 0)}},
            )
            forget(before["pool_id"])
            returned += 1
    await holds_col().update_many(
        {"status": "pending", "expires_at": {"$lt": _now()}}, {"$set": {"status": "abandoned", "released_at": _now()}},
    )
    if returned:
        logger.info("Returned %d credit holds left open", returned)
    return returned


async def reconcile(period: Optional[str] = None) -> list[dict[str, Any]]:
    """Compare every pool's balance with its ledger for the month and record any difference. Nothing is changed: a difference is
    for a person to look at. A calendar month ("2026-11") also covers the credit months of paid plans that started in it
    ("2026-11-15"), each compared on its own."""
    period = period or period_of()
    month = period[:7]
    match = {"$regex": f"^{month}"}
    totals: dict[tuple[str, str], int] = {}
    async for row in ledger_col().aggregate([
        {"$match": {"period": match, "kind": {"$in": ["charge", "refund"]}}},
        {"$group": {"_id": {"pool": "$pool_id", "period": "$period"}, "net": {"$sum": {"$cond": [{"$eq": ["$kind", "refund"]}, {"$multiply": ["$credits", -1]}, "$credits"]}}}},
    ]):
        totals[(row["_id"]["pool"], row["_id"]["period"])] = int(row["net"])
    added: dict[tuple[str, str, str], int] = {}
    async for row in ledger_col().aggregate([
        {"$match": {"period": match, "kind": {"$in": ["grant", "carry", "bought_in", "bought_back"]}}},
        {"$group": {"_id": {"pool": "$pool_id", "period": "$period", "kind": "$kind"}, "n": {"$sum": "$credits"}}},
    ]):
        added[(row["_id"]["pool"], row["_id"]["period"], row["_id"]["kind"])] = int(row["n"])
    differences = []
    async for doc in balances_col().find({"period": match}):
        key = (doc["pool_id"], doc["period"])
        expected_used = totals.get(key, 0)
        expected_granted = added.get((*key, "grant"), 0)
        expected_carried = added.get((*key, "carry"), 0)
        expected_bought = added.get((*key, "bought_in"), 0) - added.get((*key, "bought_back"), 0)
        if (int(doc.get("used") or 0) != expected_used or int(doc.get("granted") or 0) != expected_granted
                or int(doc.get("carried") or 0) != expected_carried or int(doc.get("bought_in") or 0) != expected_bought):
            differences.append({
                "pool_id": doc["pool_id"], "period": doc["period"], "used": int(doc.get("used") or 0), "ledger_used": expected_used,
                "granted": int(doc.get("granted") or 0), "ledger_granted": expected_granted,
                "carried": int(doc.get("carried") or 0), "ledger_carried": expected_carried,
                "bought_in": int(doc.get("bought_in") or 0), "ledger_bought_in": expected_bought, "at": _now(),
            })
    if differences:
        logger.warning("Credit balances differ from the ledger for %d pools in %s", len(differences), period)
        await get_db()["credit_reconcile"].insert_many([dict(d) for d in differences])
    return differences


async def housekeeping() -> None:
    """Hourly: return stale holds. The nightly ledger check runs from `nightly`."""
    await sweep()


async def reconcile_wallets() -> list[dict[str, Any]]:
    """Compare each wallet's bought credits left with its history: bought, minus moved into months, plus moved back, minus ended and
    refunded. A difference is recorded for a person to look at; nothing is changed."""
    sign = {"purchase": 1, "bought_in": -1, "bought_back": 1, "expire": -1, "clawback": -1}
    expected: dict[str, int] = {}
    async for row in ledger_col().aggregate([
        {"$match": {"kind": {"$in": list(sign)}}}, {"$group": {"_id": {"pool": "$pool_id", "kind": "$kind"}, "n": {"$sum": "$credits"}}},
    ]):
        pool_id = row["_id"]["pool"]
        expected[pool_id] = expected.get(pool_id, 0) + sign[row["_id"]["kind"]] * int(row["n"])
    differences = []
    async for wallet in wallets_col().find({}):
        left = sum(int(lot.get("left") or 0) for lot in wallet.get("lots") or [])
        if left != expected.get(wallet["_id"], 0):
            differences.append({"pool_id": wallet["_id"], "period": "wallet", "bought_left": left, "ledger_bought_left": expected.get(wallet["_id"], 0), "at": _now()})
    if differences:
        logger.warning("Bought credits differ from the history for %d pools", len(differences))
        await get_db()["credit_reconcile"].insert_many([dict(d) for d in differences])
    return differences


async def nightly() -> None:
    await sweep()
    await expire_bought()
    await reconcile()
    await reconcile_wallets()
    now = _now()
    if now.day == 1:  # the first night of a month also closes the month before
        await reconcile(period_of(now - timedelta(days=1)))


async def create_indexes() -> None:
    db = get_db()
    await db["credit_balances"].create_index([("pool_id", 1), ("period", 1)])
    await db["credit_balances"].create_index("period")
    await db["credit_holds"].create_index([("status", 1), ("expires_at", 1)])
    await db["credit_holds"].create_index("created_at", expireAfterSeconds=90 * 24 * 3600)
    await db["credit_ledger"].create_index("key", unique=True)
    await db["credit_wallets"].create_index("lots.expires_at")
    await db["credit_ledger"].create_index([("pool_id", 1), ("period", 1), ("at", -1)])
    await db["credit_ledger"].create_index([("period", 1), ("action", 1)])
    await db["credit_reconcile"].create_index([("at", -1)])
    await db["billing_credit_costs"].create_index("id", unique=True)


# ── holds kept on a record, for work another process finishes ────────────

_HOLD_FIELDS = ("action", "credits", "units", "workspace_id", "user_id", "pool_id", "period", "key", "run_id", "duplicate")


def hold_to_doc(hold: Hold) -> Optional[dict[str, Any]]:
    """What to store on a job so whoever finishes it (a worker reporting back, the reaper) can settle or release the hold."""
    if not hold.key or hold.duplicate:
        return None
    return {k: getattr(hold, k) for k in _HOLD_FIELDS}


def hold_from_doc(doc: Optional[dict[str, Any]]) -> Optional[Hold]:
    if not doc or not doc.get("key"):
        return None
    return Hold(**{k: doc.get(k) for k in _HOLD_FIELDS})


async def settle_doc(doc: Optional[dict[str, Any]], units: Optional[float] = None) -> int:
    hold = hold_from_doc(doc)
    return await settle(hold, units) if hold else 0


async def release_doc(doc: Optional[dict[str, Any]]) -> None:
    hold = hold_from_doc(doc)
    if hold:
        await release(hold)
