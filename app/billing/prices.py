"""What each paid plan and each credit pack costs: amounts in the smallest unit of the currency (paise, cents), never floating point.

The prices live in one price book that Ops edits (`billing_settings`, document "prices"); the values below are only its starting
point. Every server reads the saved book through `load_book`, cached for a few seconds, and the price functions read the book loaded
last. A changed price applies to new checkouts and changes only: a running subscription keeps the price it was bought at, because a
provider plan's amount never changes.

Prices are per plan, currency and billing period. Large is billed per seat: the subscription's quantity is the seat count, and from
`volume_from_seats` seats the lower seat price applies. The first `founding_limit` paying workspaces get the founding price and keep it
while their subscription stays active. A yearly plan costs `yearly_months` months.

Monthly plans renew on a billing day the owner picks (1 to 28, the 1st unless they choose another); the first payment covers the days
until then. Yearly plans are paid in full on the day and renew a year later.

Paid plans open in India first (INR). USD prices are here so the plan picker can show them to people abroad, who join a waiting list
until a merchant of record handles tax there; `CHECKOUT_CURRENCIES` is what checkout accepts today.
"""
from __future__ import annotations

import copy
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

Currency = Literal["INR", "USD"]
Period = Literal["monthly", "yearly"]

PAID_PLANS: tuple[str, ...] = ("single", "duo", "large")
CURRENCIES: tuple[Currency, ...] = ("INR", "USD")
CHECKOUT_CURRENCIES: tuple[Currency, ...] = ("INR",)
PERIODS: tuple[Period, ...] = ("monthly", "yearly")

DEFAULT_BOOK: dict[str, Any] = {
    # the monthly price of one plan (one seat on Large)
    "monthly": {"single": {"INR": 99_900, "USD": 1_900}, "duo": {"INR": 189_900, "USD": 3_500}, "large": {"INR": 89_900, "USD": 1_500}},
    "founding": {"single": {"INR": 79_900, "USD": 1_500}, "duo": {"INR": 159_900, "USD": 2_900}, "large": {"INR": 74_900, "USD": 1_200}},
    # one Large seat a month from `volume_from_seats` seats
    "large_volume": {"INR": 79_900, "USD": 1_300},
    "volume_from_seats": 25,
    "founding_limit": 100,
    # a yearly plan is ten months' price: two months free
    "yearly_months": 10,
    # credit packs a paid plan can buy when its monthly credits run short
    "packs": [
        {"id": "credits-500", "credits": 500, "prices": {"INR": 24_900}, "active": True},
        {"id": "credits-1000", "credits": 1_000, "prices": {"INR": 44_900}, "active": True},
        {"id": "credits-2500", "credits": 2_500, "prices": {"INR": 99_900}, "active": True},
    ],
}

BOOK_CACHE_SECONDS = 10
MIN_AMOUNT = 100   # one rupee or one cent, the smallest charge a provider takes
MAX_PACKS = 10

_book: dict[str, Any] = copy.deepcopy(DEFAULT_BOOK)
_loaded_at = 0.0


def book() -> dict[str, Any]:
    """The price book loaded last (the starting values until `load_book` has run)."""
    return _book


def _merge(saved: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULT_BOOK)
    for key in ("monthly", "founding"):
        for plan in PAID_PLANS:
            out[key][plan].update({c: int(v) for c, v in ((saved.get(key) or {}).get(plan) or {}).items() if c in CURRENCIES})
    out["large_volume"].update({c: int(v) for c, v in (saved.get("large_volume") or {}).items() if c in CURRENCIES})
    for key in ("volume_from_seats", "founding_limit", "yearly_months"):
        if isinstance(saved.get(key), int) and not isinstance(saved.get(key), bool):
            out[key] = saved[key]
    if isinstance(saved.get("packs"), list):
        out["packs"] = saved["packs"]
    return out


async def load_book(force: bool = False) -> dict[str, Any]:
    """Read the saved price book (every few seconds at most) and make it the one the price functions use."""
    global _book, _loaded_at
    if not force and time.monotonic() < _loaded_at:
        return _book
    from app.db.mongo import get_db

    try:
        saved = await get_db()["billing_settings"].find_one({"_id": "prices"}, {"_id": 0}) or {}
    except Exception:  # noqa: BLE001 - the book loaded last is a safe answer
        return _book
    _book = _merge(saved.get("book") or {})
    _loaded_at = time.monotonic() + BOOK_CACHE_SECONDS
    return _book


def validate_book(candidate: dict[str, Any]) -> dict[str, Any]:
    """A price book as saved from Ops, checked. Raises ValueError with a plain reason."""
    try:
        merged = _merge(candidate)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Every price is a whole number in paise or cents.") from exc
    for key, label in (("monthly", "price"), ("founding", "founding price")):
        for plan in PAID_PLANS:
            for currency in CURRENCIES:
                if merged[key][plan][currency] < MIN_AMOUNT:
                    raise ValueError(f"The {plan.title()} {label} in {currency} must be at least {format_amount(MIN_AMOUNT, currency)}.")
    for plan in PAID_PLANS:
        for currency in CURRENCIES:
            if merged["founding"][plan][currency] > merged["monthly"][plan][currency]:
                raise ValueError(f"The {plan.title()} founding price in {currency} cannot be above its usual price.")
    for currency in CURRENCIES:
        volume = merged["large_volume"][currency]
        if not MIN_AMOUNT <= volume <= merged["monthly"]["large"][currency]:
            raise ValueError(f"The lower Large seat price in {currency} must be between {format_amount(MIN_AMOUNT, currency)} and the usual seat price.")
    if not 2 <= merged["volume_from_seats"] <= 100:
        raise ValueError("The lower Large seat price starts somewhere from 2 to 100 seats.")
    if not 0 <= merged["founding_limit"] <= 100_000:
        raise ValueError("Founding places must be from 0 to 100,000.")
    if not 1 <= merged["yearly_months"] <= 12:
        raise ValueError("A yearly plan costs from 1 to 12 months.")
    raw = merged["packs"]
    if len(raw) > MAX_PACKS:
        raise ValueError(f"Up to {MAX_PACKS} credit packs.")
    seen: set[str] = set()
    clean = []
    for item in raw:
        item = item if isinstance(item, dict) else {}
        pid = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9-]{3,40}", pid) or pid in seen:
            raise ValueError("Each credit pack needs its own short name of small letters, numbers and dashes.")
        seen.add(pid)
        credits = item.get("credits")
        if not isinstance(credits, int) or isinstance(credits, bool) or not 1 <= credits <= 1_000_000:
            raise ValueError(f"Pack {pid}: credits must be a whole number from 1 to 1,000,000.")
        amounts = {c: int(v) for c, v in (item.get("prices") or {}).items() if c in CURRENCIES}
        if amounts.get("INR", 0) < MIN_AMOUNT:
            raise ValueError(f"Pack {pid}: the price in INR must be at least {format_amount(MIN_AMOUNT, 'INR')}.")
        clean.append({"id": pid, "credits": credits, "prices": amounts, "active": bool(item.get("active", True))})
    merged["packs"] = clean
    return merged


async def save_book(candidate: dict[str, Any], *, by: str, reason: str) -> dict[str, Any]:
    """Check and save a new price book, and record who changed what and why."""
    from app.db.mongo import get_db

    before = copy.deepcopy(await load_book(force=True))
    checked = validate_book(candidate)
    now = datetime.now(timezone.utc)
    await get_db()["billing_settings"].update_one({"_id": "prices"}, {"$set": {"book": checked, "updated_at": now, "updated_by": by}}, upsert=True)
    await get_db()["billing_audit"].insert_one({
        "at": now, "by": by, "by_email": by, "action": "prices_changed", "target": "prices", "target_name": "Prices",
        "before": before, "after": checked, "reason": reason,
    })
    return await load_book(force=True)


def founding_limit() -> int:
    return int(_book["founding_limit"])


def volume_from_seats() -> int:
    return int(_book["volume_from_seats"])


def yearly_months() -> int:
    return int(_book["yearly_months"])


def packs(*, active_only: bool = True) -> list[dict[str, Any]]:
    return [p for p in _book["packs"] if p.get("active") or not active_only]


def pack(pack_id: str) -> Optional[dict[str, Any]]:
    return next((p for p in packs() if p["id"] == pack_id), None)


SYMBOL: dict[Currency, str] = {"INR": "Rs", "USD": "$"}


def is_paid(plan: str) -> bool:
    return plan in PAID_PLANS


def unit_amount(plan: str, currency: Currency, period: Period, *, seats: int = 1, founding: bool = False) -> int:
    """The price of one unit for one billing period: the plan, or one seat on Large."""
    if plan not in PAID_PLANS or currency not in CURRENCIES or period not in PERIODS:
        raise ValueError(f"No price for {plan} in {currency}, {period}.")
    monthly = int(_book["monthly"][plan][currency])
    if plan == "large" and seats >= volume_from_seats():
        monthly = min(monthly, int(_book["large_volume"][currency]))
    if founding:
        monthly = min(monthly, int(_book["founding"][plan][currency]))
    return monthly * (yearly_months() if period == "yearly" else 1)


def quantity_for(plan: str, seats: int) -> int:
    """How many units a subscription carries: the seat count on Large, one otherwise."""
    return max(1, int(seats)) if plan == "large" else 1


def total(plan: str, currency: Currency, period: Period, *, seats: int = 1, founding: bool = False) -> int:
    """What one billing period costs in all."""
    return unit_amount(plan, currency, period, seats=seats, founding=founding) * quantity_for(plan, seats)


def price_key(plan: str, currency: Currency, period: Period, amount: int) -> str:
    """A name for one price, so the provider plan made for it is reused. A changed amount is a new key, because a provider plan's
    amount can never change."""
    return f"{plan}-{currency}-{period}-{amount}"


def format_amount(amount: int, currency: Currency) -> str:
    """An amount as a person reads it: Rs 999, Rs 18,990, $19, $13.50."""
    whole, part = divmod(int(amount), 100)
    text = f"{whole:,}" + (f".{part:02d}" if part else "")
    return f"{SYMBOL[currency]} {text}" if currency == "INR" else f"{SYMBOL[currency]}{text}"


def table(currency: Currency, *, founding: bool = False) -> list[dict]:
    """Every paid plan's prices in one currency, for the plan picker."""
    rows = []
    for plan in PAID_PLANS:
        row: dict = {"plan": plan, "currency": currency, "per_seat": plan == "large"}
        for period in PERIODS:
            amount = unit_amount(plan, currency, period, founding=founding)
            row[period] = {"amount": amount, "label": format_amount(amount, currency)}
            if plan == "large":
                volume = unit_amount(plan, currency, period, seats=volume_from_seats(), founding=founding)
                row[period]["volume_amount"] = volume
                row[period]["volume_label"] = format_amount(volume, currency)
        rows.append(row)
    return rows


BILLING_DAYS = range(1, 29)


def _month_shift(day_in: date, months: int, day: int) -> date:
    index = day_in.year * 12 + day_in.month - 1 + months
    return date(index // 12, index % 12 + 1, day)


def next_billing(today: date, billing_day: int = 1) -> date:
    """The next date a monthly plan renews: the chosen day of the month (1 to 28), after today."""
    if not 1 <= billing_day <= 28:
        raise ValueError("Choose a billing day from 1 to 28.")
    if today.day < billing_day:
        return date(today.year, today.month, billing_day)
    return _month_shift(today, 1, billing_day)


def part_month(monthly_total: int, today: date, billing_day: int = 1) -> int:
    """What the days from today until the first renewal cost, rounded to a whole rupee or dollar. Bought on the billing day itself, it
    is a whole month."""
    renews = next_billing(today, billing_day)
    cycle = (renews - _month_shift(renews, -1, billing_day)).days
    days_left = (renews - today).days
    return max(100, round(monthly_total * days_left / cycle / 100) * 100)


def quote(plan: str, currency: Currency, period: Period, *, seats: int, founding: bool, today: date, billing_day: int = 1) -> dict:
    """What someone pays today and at each renewal. A monthly plan renews on the billing day the owner chose (the 1st unless they pick
    another), so the first payment covers the days until then; a yearly plan is paid in full today and renews a year later."""
    renew = total(plan, currency, period, seats=seats, founding=founding)
    if period == "monthly":
        renews_on = next_billing(today, billing_day)
        today_amount = part_month(renew, today, billing_day)
    else:
        # the same date next year, as the provider renews it; 29 February falls back to the 28th
        renews_on = date(today.year + 1, today.month, today.day if (today.month, today.day) != (2, 29) else 28)
        today_amount = renew
    return {
        "today": today_amount, "today_label": format_amount(today_amount, currency),
        "renew": renew, "renew_label": format_amount(renew, currency),
        "renews_on": renews_on.isoformat(), "part_month": period == "monthly" and today_amount != renew,
        "billing_day": billing_day if period == "monthly" else None,
        "currency": currency, "founding": founding,
    }


def currency_for_country(country: Optional[str]) -> Currency:
    return "INR" if (country or "").upper() in ("IN", "IND", "INDIA") else "USD"
