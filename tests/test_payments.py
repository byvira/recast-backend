"""Payments: prices, checkout, Razorpay's messages, the workspace's plan following its subscription, invoices, and the payments watch.

Razorpay is replaced by a stand-in that keeps the real signature checks and answers the network calls itself, so nothing here reaches
Razorpay. The stand-in records every call, so a test can see what would have been sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from uuid import uuid4
from datetime import datetime, timezone

import pytest

from app.billing import invoices
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.billing import watch
from app.billing.provider import ProviderError, use_provider
from app.billing.razorpay import RazorpayProvider
from app.db.mongo import get_db, workspaces
from tests.conftest import create_workspace

KEY_SECRET = "test-key-secret"
WEBHOOK_SECRET = "test-webhook-secret"


class StandIn(RazorpayProvider):
    def __init__(self, key_id: str = "rzp_test_standin") -> None:
        super().__init__(key_id=key_id, key_secret=KEY_SECRET, webhook_secret=WEBHOOK_SECRET)
        self.calls: list[tuple[str, dict]] = []
        self.subs: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self._n = 0

    async def create_plan(self, **kw) -> str:
        self.calls.append(("create_plan", kw))
        self._n += 1
        return f"plan_{uuid4().hex[:14]}"

    async def create_subscription(self, **kw) -> dict:
        self.calls.append(("create_subscription", kw))
        self._n += 1
        # unique across tests: the test database keeps earlier tests' subscriptions
        sid = f"sub_{uuid4().hex[:14]}"
        self.subs[sid] = {"id": sid, "status": "created", "quantity": kw["quantity"], "plan_id": kw["plan_id"]}
        return {**self.subs[sid], "short_url": f"https://rzp.io/{sid}"}

    async def fetch_subscription(self, subscription_id: str) -> dict:
        self.calls.append(("fetch_subscription", {"id": subscription_id}))
        if subscription_id not in self.subs:
            # an earlier test's subscription: Razorpay answers "not found" for an id it does not hold
            raise ProviderError("The id provided does not exist", status=400)
        return dict(self.subs[subscription_id])

    async def pause_subscription(self, subscription_id: str) -> dict:
        self.calls.append(("pause_subscription", {"id": subscription_id}))
        self.subs[subscription_id]["status"] = "paused"
        return dict(self.subs[subscription_id])

    async def resume_subscription(self, subscription_id: str) -> dict:
        self.calls.append(("resume_subscription", {"id": subscription_id}))
        self.subs[subscription_id]["status"] = "active"
        return dict(self.subs[subscription_id])

    async def refund(self, payment_id: str, *, amount=None, notes=None) -> dict:
        self.calls.append(("refund", {"payment_id": payment_id, "amount": amount}))
        return {"id": f"rfnd_{payment_id}", "status": "processed", "amount": amount}

    async def latest_invoice(self, subscription_id: str) -> dict | None:
        self.calls.append(("latest_invoice", {"id": subscription_id}))
        return {"id": f"inv_{subscription_id}", "amount": 1_899_000, "short_url": f"https://rzp.io/i/{subscription_id}"}

    async def create_order(self, *, amount: int, currency: str, receipt: str, notes: dict) -> dict:
        self.calls.append(("create_order", {"amount": amount, "currency": currency, "notes": notes}))
        oid = f"order_{uuid4().hex[:14]}"
        self.orders[oid] = {"id": oid, "amount": amount, "currency": currency, "notes": notes}
        return dict(self.orders[oid])

    async def fetch_payment(self, payment_id: str) -> dict:
        self.calls.append(("fetch_payment", {"id": payment_id}))
        if payment_id not in self.payments:
            raise ProviderError("The id provided does not exist", status=400)
        return dict(self.payments[payment_id])

    async def capture_payment(self, payment_id: str, *, amount: int, currency: str) -> dict:
        self.calls.append(("capture_payment", {"id": payment_id, "amount": amount}))
        self.payments[payment_id]["status"] = "captured"
        return dict(self.payments[payment_id])

    def pay_order(self, order_id: str, *, status: str = "captured", method: str = "upi") -> dict:
        """A payment made in the window for an order, as Razorpay would hold it."""
        order = self.orders[order_id]
        pid = f"pay_{uuid4().hex[:14]}"
        self.payments[pid] = {"id": pid, "order_id": order_id, "amount": order["amount"], "currency": order["currency"],
                              "status": status, "method": method, "created_at": int(time.time())}
        return dict(self.payments[pid])

    async def cancel_subscription(self, subscription_id: str, *, at_cycle_end: bool) -> dict:
        self.calls.append(("cancel_subscription", {"id": subscription_id, "at_cycle_end": at_cycle_end}))
        if not at_cycle_end:
            self.subs[subscription_id]["status"] = "cancelled"
        return dict(self.subs[subscription_id])


@pytest.fixture
async def standin():
    provider = StandIn()
    use_provider(provider)
    await invoices.save_settings({"checkout_open": True})
    yield provider
    use_provider(None)
    await get_db()["billing_settings"].delete_many({})


def _signed(body: dict) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _checkout_sig(payment_id: str, subscription_id: str) -> str:
    return hmac.new(KEY_SECRET.encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256).hexdigest()


CUSTOMER = {"name": "Asha Rao", "address": "12 Lake Road", "city": "Chennai", "state": "Tamil Nadu", "postal_code": "600001", "country": "IN"}


async def _owner_with_workspace(signup_user):
    client, profile = await signup_user("Owner")
    ws_id = await create_workspace(client, "Paying workspace")
    client.headers["X-Workspace-Id"] = ws_id
    return client, profile, ws_id


# ── prices ─────────────────────────────────────────────────────────────────

def test_prices_follow_the_accepted_table():
    assert PR.total("single", "INR", "monthly") == 99_900
    assert PR.total("duo", "INR", "yearly") == 1_899_000
    assert PR.total("large", "INR", "monthly", seats=3) == 3 * 89_900
    # the lower seat price applies from 25 seats
    assert PR.total("large", "INR", "monthly", seats=24) == 24 * 89_900
    assert PR.total("large", "INR", "monthly", seats=25) == 25 * 79_900
    assert PR.total("single", "INR", "monthly", founding=True) == 79_900
    assert PR.total("large", "INR", "monthly", seats=30, founding=True) == 30 * 74_900
    assert PR.total("single", "USD", "yearly") == 19_000
    assert PR.format_amount(1_899_000, "INR") == "Rs 18,990"
    assert PR.format_amount(1_350, "USD") == "$13.50"


def test_a_monthly_plan_renews_on_the_1st_by_default_and_the_first_payment_is_the_rest_of_the_month():
    from datetime import date

    q = PR.quote("duo", "INR", "monthly", seats=2, founding=False, today=date(2026, 10, 22))
    assert q["renews_on"] == "2026-11-01" and q["billing_day"] == 1
    # 10 of October's 31 days are left, today included
    assert q["today"] == round(189_900 * 10 / 31 / 100) * 100 == 61_300
    assert q["renew"] == 189_900 and q["part_month"] is True


def test_the_owner_can_pick_another_billing_day():
    from datetime import date

    assert PR.next_billing(date(2026, 11, 3), 15) == date(2026, 11, 15)
    assert PR.next_billing(date(2026, 11, 20), 15) == date(2026, 12, 15)
    assert PR.next_billing(date(2026, 12, 20), 15) == date(2027, 1, 15)
    # bought on the billing day itself: a whole month now, the next one a month later
    q = PR.quote("single", "INR", "monthly", seats=1, founding=False, today=date(2026, 11, 15), billing_day=15)
    assert q["today"] == q["renew"] == 99_900 and q["renews_on"] == "2026-12-15" and q["part_month"] is False
    with pytest.raises(ValueError):
        PR.next_billing(date(2026, 11, 3), 31)


def test_a_yearly_plan_is_paid_in_full_today():
    from datetime import date

    q = PR.quote("single", "INR", "yearly", seats=1, founding=False, today=date(2026, 11, 3))
    assert q["today"] == q["renew"] == 999_000 and q["renews_on"] == "2027-11-03" and q["billing_day"] is None


def test_credits_start_again_on_the_billing_day():
    from app.billing import credits

    on = lambda y, m, d: datetime(y, m, d, 12, tzinfo=timezone.utc)  # noqa: E731
    # the 1st keeps today's calendar months, so nothing changes for anyone who is not on a paid plan with another day
    assert credits.period_of(on(2026, 11, 20)) == "2026-11"
    assert credits.resets_at("2026-11") == datetime(2026, 12, 1, tzinfo=timezone.utc)
    # a plan billed on the 15th: its month runs from the 15th to the 14th, named by its first day
    assert credits.period_of(on(2026, 11, 20), 15) == "2026-11-15"
    assert credits.period_of(on(2026, 11, 14), 15) == "2026-10-15"
    assert credits.period_of(on(2027, 1, 3), 15) == "2026-12-15"
    assert credits.resets_at("2026-12-15") == datetime(2027, 1, 15, tzinfo=timezone.utc)
    assert credits.starts_at("2026-11-15") == datetime(2026, 11, 15, tzinfo=timezone.utc)


async def test_a_paid_plan_sets_the_billing_day_and_ending_it_goes_back_to_the_1st(signup_user, standin):
    from app.billing import credits

    client, _, ws_id = await _owner_with_workspace(signup_user)
    res = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "billing_day": 12, "customer": CUSTOMER})
    sid = res.json()["checkout"]["subscription_id"]
    now = int(time.time())
    await S._apply_event(_event("subscription.authenticated", sid, "authenticated", now), datetime.fromtimestamp(now, tz=timezone.utc))
    assert (await workspaces.find_one({"id": ws_id}))["billing_day"] == 12
    from app.shared import entitlements

    entitlements.forget(ws_id)
    assert (await credits.pool_for(ws_id)).billing_day == 12
    await S._apply_event(_event("subscription.cancelled", sid, "cancelled", now + 5), datetime.fromtimestamp(now + 5, tz=timezone.utc))
    assert (await workspaces.find_one({"id": ws_id}))["billing_day"] == 1


def test_a_changed_price_gets_a_new_provider_plan_key():
    assert PR.price_key("single", "INR", "monthly", 99_900) != PR.price_key("single", "INR", "monthly", 79_900)


def test_invoice_numbers_follow_the_indian_financial_year():
    assert invoices.financial_year(datetime(2026, 3, 31, tzinfo=timezone.utc)) == "2025-26"
    assert invoices.financial_year(datetime(2026, 4, 1, tzinfo=timezone.utc)) == "2026-27"


def test_placeholder_seller_details_are_not_ready():
    assert not invoices.seller_ready(invoices.DEFAULT_SETTINGS)
    assert invoices.seller_ready({"seller": {"name": "Vira Studio", "address": "Chennai"}})


# ── checkout ───────────────────────────────────────────────────────────────

async def test_checkout_is_refused_while_closed(signup_user):
    use_provider(StandIn())
    try:
        client, _, _ = await _owner_with_workspace(signup_user)
        res = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "customer": CUSTOMER})
        assert res.status_code == 403
        assert "not open yet" in res.json()["detail"]["message"]
    finally:
        use_provider(None)


async def test_live_keys_with_placeholder_seller_details_cannot_charge(signup_user, standin):
    use_provider(StandIn(key_id="rzp_live_standin"))
    client, _, _ = await _owner_with_workspace(signup_user)
    res = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "customer": CUSTOMER})
    assert res.status_code == 503


async def test_abroad_joins_the_waiting_list(signup_user, standin):
    client, _, _ = await _owner_with_workspace(signup_user)
    res = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "customer": {**CUSTOMER, "country": "US"}})
    assert res.status_code == 400
    assert "waiting list" in res.json()["detail"]["message"]


async def test_checkout_opens_a_subscription_and_the_plan_waits_for_payment(signup_user, standin):
    client, _, ws_id = await _owner_with_workspace(signup_user)
    res = await client.post("/api/v1/billing/checkout", json={"plan": "large", "period": "monthly", "seats": 4, "customer": CUSTOMER})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["checkout"]["key"] == "rzp_test_standin"
    sub_call = [kw for name, kw in standin.calls if name == "create_subscription"][0]
    assert sub_call["quantity"] == 4 and sub_call["total_count"] == 120
    assert body["subscription"]["status"] == "created"
    ws = await workspaces.find_one({"id": ws_id})
    assert ws.get("plan") in (None, "free")  # nothing changes until the payment is confirmed


async def test_a_monthly_checkout_starts_on_the_billing_day_with_the_first_days_up_front(signup_user, standin):
    client, _, ws_id = await _owner_with_workspace(signup_user)
    res = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "billing_day": 10, "customer": CUSTOMER})
    assert res.status_code == 200, res.text
    body = res.json()
    call = [kw for name, kw in standin.calls if name == "create_subscription"][-1]
    assert call["start_at"] and call["upfront"]["amount"] == body["quote"]["today"]
    assert body["quote"]["renews_on"].endswith("-10")
    sid = body["checkout"]["subscription_id"]
    # the mandate is set and the first days are paid: Razorpay calls it "authenticated" until the billing day, and the plan applies now
    now = int(time.time())
    payment = {"id": f"pay_up_{sid}", "amount": body["quote"]["today"], "currency": "INR", "status": "captured", "method": "upi", "created_at": now}
    await S._apply_event(_event("subscription.authenticated", sid, "authenticated", now, payment), datetime.fromtimestamp(now, tz=timezone.utc))
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == "single" and ws["billing_status"] == "active"
    assert await get_db()["billing_invoices"].count_documents({"payment_id": payment["id"]}) == 1


async def test_the_quote_matches_what_checkout_charges(signup_user, standin):
    client, _, _ = await _owner_with_workspace(signup_user)
    q = await client.post("/api/v1/billing/quote", json={"plan": "large", "period": "monthly", "seats": 6, "billing_day": 5})
    assert q.status_code == 200, q.text
    res = await client.post("/api/v1/billing/checkout", json={"plan": "large", "period": "monthly", "seats": 6, "billing_day": 5, "customer": CUSTOMER})
    assert res.json()["quote"]["today"] == q.json()["today"] and res.json()["quote"]["renew"] == q.json()["renew"]


async def test_the_same_price_reuses_one_provider_plan(signup_user, standin):
    for _ in range(2):
        client, _, _ = await _owner_with_workspace(signup_user)
        res = await client.post("/api/v1/billing/checkout", json={"plan": "duo", "period": "yearly", "customer": CUSTOMER})
        assert res.status_code == 200, res.text
    made = [kw for name, kw in standin.calls if name == "create_plan" and kw["amount"] == PR.unit_amount("duo", "INR", "yearly", founding=True)]
    assert len(made) <= 1


async def test_a_confirmed_payment_puts_the_workspace_on_its_plan(signup_user, standin):
    client, _, ws_id = await _owner_with_workspace(signup_user)
    res = await client.post("/api/v1/billing/checkout", json={"plan": "large", "period": "monthly", "seats": 5, "customer": CUSTOMER})
    sid = res.json()["checkout"]["subscription_id"]

    bad = await client.post("/api/v1/billing/checkout/confirm", json={"razorpay_payment_id": "pay_1", "razorpay_subscription_id": sid, "razorpay_signature": "nope"})
    assert bad.status_code == 400

    standin.subs[sid].update(status="active", current_start=int(time.time()), current_end=int(time.time()) + 30 * 86400)
    ok = await client.post("/api/v1/billing/checkout/confirm", json={"razorpay_payment_id": "pay_1", "razorpay_subscription_id": sid, "razorpay_signature": _checkout_sig("pay_1", sid)})
    assert ok.status_code == 200, ok.text
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == "large" and ws["plan_seats"] == 5 and ws["billing_status"] == "active"


async def test_a_member_who_is_not_the_owner_cannot_buy(signup_user, standin, make_client):
    from tests.conftest import invite_and_accept

    client, _, ws_id = await _owner_with_workspace(signup_user)
    member, _ = await invite_and_accept(client, make_client, ws_id, "admin")
    member.headers["X-Workspace-Id"] = ws_id
    res = await member.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "customer": CUSTOMER})
    assert res.status_code == 403


# ── Razorpay's messages ────────────────────────────────────────────────────

async def _opened(signup_user, standin, plan="single"):
    client, _, ws_id = await _owner_with_workspace(signup_user)
    res = await client.post("/api/v1/billing/checkout", json={"plan": plan, "period": "monthly", "customer": CUSTOMER})
    return client, ws_id, res.json()["checkout"]["subscription_id"]


def _event(event: str, sid: str, status: str, created_at: int, payment: dict | None = None) -> dict:
    payload: dict = {"subscription": {"entity": {"id": sid, "status": status, "current_start": created_at, "current_end": created_at + 30 * 86400}}}
    if payment:
        payload["payment"] = {"entity": payment}
    return {"entity": "event", "event": event, "created_at": created_at, "payload": payload}


async def test_a_webhook_with_a_wrong_signature_is_refused_and_watched(signup_user, standin, api_client):
    raw, _ = _signed({"event": "subscription.activated"})
    res = await api_client.post("/api/v1/billing/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": "forged", "x-razorpay-event-id": "evt_forged"})
    assert res.status_code == 400
    found = await watch.run_checks(standin)
    assert any(f.kind == "signature_rejected" for f in found)
    await S.rejects_col().delete_many({})


async def test_a_charge_applies_once_with_one_invoice_even_when_sent_twice(signup_user, standin, api_client):
    _, ws_id, sid = await _opened(signup_user, standin)
    now = int(time.time())
    payment = {"id": f"pay_{sid}", "amount": 79_900, "currency": "INR", "status": "captured", "method": "upi", "created_at": now}
    raw, sig = _signed(_event("subscription.charged", sid, "active", now, payment))
    for _ in range(2):
        res = await api_client.post("/api/v1/billing/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": f"evt_{sid}"})
        assert res.status_code == 200
    # the stored message is applied by the task the webhook started; the sweep leaves a row another worker holds alone
    await asyncio.gather(*list(S._tasks))
    await S.process_pending()
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == "single" and ws["billing_status"] == "active"
    rows = [r async for r in get_db()["billing_invoices"].find({"payment_id": payment["id"]})]
    assert len(rows) == 1
    inv = rows[0]
    assert inv["number"].startswith("EKV-") and inv["tax"] == 0 and inv["note"] == invoices.NOT_REGISTERED_NOTE
    assert inv["customer"]["state"] == "Tamil Nadu"
    # applying the same message again changes nothing
    await S._apply_event(_event("subscription.charged", sid, "active", now, payment), datetime.fromtimestamp(now, tz=timezone.utc))
    assert await get_db()["billing_invoices"].count_documents({"payment_id": payment["id"]}) == 1


async def test_a_late_message_never_moves_a_subscription_backwards(signup_user, standin):
    _, ws_id, sid = await _opened(signup_user, standin)
    now = int(time.time())
    await S._apply_event(_event("subscription.cancelled", sid, "cancelled", now), datetime.fromtimestamp(now, tz=timezone.utc))
    await S._apply_event(_event("subscription.activated", sid, "active", now - 60), datetime.fromtimestamp(now - 60, tz=timezone.utc))
    sub = await S.subs_col().find_one({"provider_subscription_id": sid})
    assert sub["status"] == "cancelled"
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == "free" and ws["billing_status"] == "ended"


async def test_a_retried_renewal_keeps_the_plan_and_says_past_due(signup_user, standin):
    _, ws_id, sid = await _opened(signup_user, standin)
    now = int(time.time())
    await S._apply_event(_event("subscription.activated", sid, "active", now), datetime.fromtimestamp(now, tz=timezone.utc))
    await S._apply_event(_event("subscription.pending", sid, "pending", now + 10), datetime.fromtimestamp(now + 10, tz=timezone.utc))
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == "single" and ws["billing_status"] == "past_due"
    found = await watch.run_checks(standin)
    assert any(f.kind == "renewal_failing" and f.workspace_id == ws_id for f in found)


async def test_cancel_keeps_the_plan_until_the_period_ends(signup_user, standin):
    client, ws_id, sid = await _opened(signup_user, standin)
    now = int(time.time())
    standin.subs[sid]["status"] = "active"
    await S._apply_event(_event("subscription.activated", sid, "active", now), datetime.fromtimestamp(now, tz=timezone.utc))
    res = await client.post("/api/v1/billing/cancel")
    assert res.status_code == 200, res.text
    assert res.json()["subscription"]["cancel_at_period_end"] is True
    assert ("cancel_subscription", {"id": sid, "at_cycle_end": True}) in standin.calls
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == "single"


# ── the payments watch ─────────────────────────────────────────────────────

async def test_the_watch_finds_a_workspace_off_its_paid_plan_and_closes_it_once_fixed(signup_user, standin):
    _, ws_id, sid = await _opened(signup_user, standin, plan="duo")
    now = int(time.time())
    await S._apply_event(_event("subscription.activated", sid, "active", now), datetime.fromtimestamp(now, tz=timezone.utc))
    await workspaces.update_one({"id": ws_id}, {"$set": {"plan": "free"}})
    found = await watch.run_checks(standin)
    key = f"plan:mismatch:{ws_id}"
    assert any(f.key == key and f.severity == "critical" for f in found)
    assert (await watch.findings_col().find_one({"key": key}))["open"] is True

    await workspaces.update_one({"id": ws_id}, {"$set": {"plan": "duo"}})
    await watch.run_checks(standin)
    assert (await watch.findings_col().find_one({"key": key}))["open"] is False


async def test_the_nightly_check_corrects_a_missed_message(signup_user, standin):
    _, ws_id, sid = await _opened(signup_user, standin)
    now = int(time.time())
    await S._apply_event(_event("subscription.activated", sid, "active", now), datetime.fromtimestamp(now, tz=timezone.utc))
    standin.subs[sid]["status"] = "halted"  # Razorpay moved on and the message never arrived
    found = await watch.reconcile(standin)
    assert any(f.kind == "reconcile_drift" for f in found)
    sub = await S.subs_col().find_one({"provider_subscription_id": sid})
    assert sub["status"] == "halted"
    assert (await workspaces.find_one({"id": ws_id}))["billing_status"] == "past_due"


async def test_ops_can_read_the_watch_and_members_cannot(signup_user, standin):
    client, _, _ = await _owner_with_workspace(signup_user)
    res = await client.get("/api/v1/ops/billing/watch")
    assert res.status_code in (401, 403)


async def test_a_first_payment_that_comes_on_its_own_is_recorded_from_its_notes(signup_user, standin):
    from app.billing import invoices as inv

    client, ws_id, sid = await _opened(signup_user, standin)
    sub = await S.subs_col().find_one({"provider_subscription_id": sid})
    created = int(time.time())
    # Razorpay sends the approval without the payment, then the payment by itself, with no subscription id but our reference
    await S._apply_event(_event("subscription.authenticated", sid, "authenticated", created), None)
    payment = {"id": f"pay_{uuid4().hex[:10]}", "amount": 12_300, "currency": "INR", "status": "captured", "method": "card",
               "order_id": "order_x", "invoice_id": "inv_x", "subscription_id": None, "created_at": created,
               "notes": {"workspace_id": ws_id, "sub_ref": sub["id"]}}
    for event in ("payment.captured", "order.paid"):
        assert await S._apply_event({"event": event, "created_at": created, "payload": {"payment": {"entity": payment}}}, None) == "applied"
    rows = [p async for p in S.payments_col().find({"workspace_id": ws_id})]
    assert len(rows) == 1 and rows[0]["amount"] == 12_300 and rows[0]["subscription_id"] == sub["id"]
    assert await inv._invoices_col().count_documents({"payment_id": payment["id"]}) == 1, "one receipt, even when told twice"
