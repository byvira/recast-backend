"""A paid plan after checkout: changing plan and seats, pausing, cancelling and keeping it, the renewal reminder, the grace period and
limited mode after a failed payment, and approving a renewal the bank wants confirmed. Razorpay is the same stand-in as in
test_payments.py: real signature checks, no network."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.billing import credits, lifecycle
from app.billing import subscriptions as S
from app.db.mongo import workspaces
from app.shared import entitlements
from tests.test_payments import CUSTOMER, _event, _owner_with_workspace, standin  # noqa: F401 - the fixture is used by name


def _at(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


async def _paid(signup_user, standin, *, plan="duo", period="monthly", seats=1):  # noqa: F811
    """A workspace on a paid plan that is active, its period ending in 20 days."""
    client, _, ws_id = await _owner_with_workspace(signup_user)
    res = await client.post("/api/v1/billing/checkout", json={"plan": plan, "period": period, "seats": seats, "customer": CUSTOMER})
    assert res.status_code == 200, res.text
    sid = res.json()["checkout"]["subscription_id"]
    now = int(time.time())
    standin.subs[sid].update(status="active", current_start=now - 10 * 86400, current_end=now + 20 * 86400, charge_at=now + 20 * 86400)
    body = _event("subscription.activated", sid, "active", now)
    body["payload"]["subscription"]["entity"].update(current_start=now - 10 * 86400, current_end=now + 20 * 86400, charge_at=now + 20 * 86400)
    await S._apply_event(body, _at(now))
    return client, ws_id, sid


async def test_a_bigger_plan_applies_now_and_charges_the_difference_for_the_rest_of_the_period(signup_user, standin):  # noqa: F811
    client, ws_id, sid = await _paid(signup_user, standin, plan="single")
    q = await client.post("/api/v1/billing/change/quote", json={"plan": "duo", "period": "monthly"})
    assert q.status_code == 200, q.text
    terms = q.json()
    assert terms["applies"] == "now"
    # two thirds of the period are left, so about two thirds of the difference
    current = (await S.subs_col().find_one({"provider_subscription_id": sid}))["amount"]
    assert abs(terms["today"] - (terms["renew"] - current) * 2 / 3) < 2_000

    res = await client.post("/api/v1/billing/change", json={"plan": "duo", "period": "monthly"})
    assert res.status_code == 200, res.text
    call = [kw for name, kw in standin.calls if name == "create_subscription"][-1]
    assert call["upfront"]["amount"] == terms["today"] and call["start_at"]
    new_sid = res.json()["checkout"]["subscription_id"]

    # approved and the difference paid: the new plan applies at once, the old one ends at its renewal
    now = int(time.time())
    await S._apply_event(_event("subscription.authenticated", new_sid, "authenticated", now + 1), _at(now + 1))
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == "duo"
    assert ("cancel_subscription", {"id": sid, "at_cycle_end": True}) in standin.calls
    assert (await S.subs_col().find_one({"provider_subscription_id": sid}))["cancel_at_period_end"] is True


async def test_a_smaller_plan_waits_for_the_renewal_with_nothing_to_pay_today(signup_user, standin):  # noqa: F811
    client, ws_id, sid = await _paid(signup_user, standin, plan="duo")
    res = await client.post("/api/v1/billing/change", json={"plan": "single", "period": "monthly"})
    assert res.status_code == 200, res.text
    assert res.json()["terms"]["applies"] == "at_renewal" and res.json()["terms"]["today"] == 0
    call = [kw for name, kw in standin.calls if name == "create_subscription"][-1]
    assert call["upfront"] is None and call["start_at"]
    new_sid = res.json()["checkout"]["subscription_id"]
    now = int(time.time())
    await S._apply_event(_event("subscription.authenticated", new_sid, "authenticated", now + 1), _at(now + 1))
    # Duo is kept until the renewal
    assert (await workspaces.find_one({"id": ws_id}))["plan"] == "duo"
    sched = await client.get("/api/v1/billing/subscription")
    assert sched.json()["scheduled"]["plan"] == "single"
    # at the renewal the old one ends and the new one starts
    await S._apply_event(_event("subscription.completed", sid, "cancelled", now + 2), _at(now + 2))
    await S._apply_event(_event("subscription.activated", new_sid, "active", now + 3), _at(now + 3))
    assert (await workspaces.find_one({"id": ws_id}))["plan"] == "single"


async def test_seats_never_go_below_those_in_use(signup_user, standin):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan="large", seats=5)
    entitlements.forget()
    in_use = (await client.get("/api/v1/billing/subscription")).json()["seats_in_use"]
    assert in_use >= 1
    res = await client.post("/api/v1/billing/change", json={"plan": "large", "period": "monthly", "seats": 3})
    # the owner alone is under three, so three seats are allowed; going under the seats in use is refused
    assert res.status_code == 200 if in_use <= 3 else res.status_code == 400


async def test_switching_to_yearly_starts_at_the_renewal(signup_user, standin):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan="single")
    q = await client.post("/api/v1/billing/change/quote", json={"plan": "single", "period": "yearly"})
    assert q.json()["applies"] == "at_renewal" and q.json()["today"] == 0


async def test_the_same_plan_is_not_a_change(signup_user, standin):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan="single")
    q = await client.post("/api/v1/billing/change/quote", json={"plan": "single", "period": "monthly"})
    assert q.status_code == 400


async def test_a_pause_starts_at_the_end_of_the_paid_period_and_resumes_by_itself(signup_user, standin):  # noqa: F811
    client, ws_id, sid = await _paid(signup_user, standin, plan="single")
    res = await client.post("/api/v1/billing/pause", json={"months": 2})
    assert res.status_code == 200, res.text
    sub = await S.subs_col().find_one({"provider_subscription_id": sid})
    renews = lifecycle.renewal_at(sub)
    assert abs((sub["pause"]["pause_at"].replace(tzinfo=timezone.utc) - (renews - lifecycle.PAUSE_LEAD)).total_seconds()) < 1
    # two months after the renewal, give or take the shorter months
    assert 56 <= (sub["pause"]["resume_at"].replace(tzinfo=timezone.utc) - renews).days <= 62
    # not due yet: nothing goes to Razorpay
    await lifecycle.pause_tick(standin)
    assert not any(name == "pause_subscription" and kw["id"] == sid for name, kw in standin.calls)
    # due: paused, and the plan keeps working until the paid period ends
    await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"pause.pause_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    await lifecycle.pause_tick(standin)
    assert ("pause_subscription", {"id": sid}) in standin.calls
    assert (await workspaces.find_one({"id": ws_id}))["plan"] == "single"
    # over: resumed by itself
    await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"pause.resume_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    await lifecycle.pause_tick(standin)
    assert ("resume_subscription", {"id": sid}) in standin.calls
    assert (await S.subs_col().find_one({"id": sub["id"]}))["pause"] is None


async def test_cancel_records_why_and_keep_brings_the_plan_back_from_the_renewal(signup_user, standin):  # noqa: F811
    client, ws_id, sid = await _paid(signup_user, standin, plan="single")
    res = await client.post("/api/v1/billing/cancel", json={"reason": "too_expensive"})
    assert res.status_code == 200, res.text
    assert await lifecycle.feedback_col().count_documents({"workspace_id": ws_id, "reason": "too_expensive"}) == 1
    keep = await client.post("/api/v1/billing/keep")
    assert keep.status_code == 200, keep.text
    assert keep.json()["terms"]["applies"] == "at_renewal" and keep.json()["terms"]["today"] == 0


async def test_after_seven_days_of_a_failed_payment_new_ai_work_waits_and_paying_restores_it(signup_user, standin):  # noqa: F811
    _, ws_id, sid = await _paid(signup_user, standin, plan="single")
    now = int(time.time())
    await S._apply_event(_event("subscription.pending", sid, "pending", now + 5), _at(now + 5))
    assert (await workspaces.find_one({"id": ws_id}))["billing_status"] == "past_due"
    entitlements.forget()
    await credits.require_room(ws_id, 1)  # still works during the grace period

    await S.subs_col().update_one({"provider_subscription_id": sid}, {"$set": {"grace_until": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    await lifecycle.grace_tick()
    assert (await workspaces.find_one({"id": ws_id}))["billing_status"] == "limited"
    entitlements.forget()
    with pytest.raises(credits.CreditsExhausted) as refused:
        await credits.reserve(ws_id, None, "text_post")
    assert refused.value.detail["code"] == "billing_limited"

    await S._apply_event(_event("subscription.charged", sid, "active", now + 10), _at(now + 10))
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["billing_status"] == "active"
    assert (await S.subs_col().find_one({"provider_subscription_id": sid}))["grace_until"] is None


async def test_a_renewal_over_the_bank_limit_opens_an_approval_link_that_closes_when_paid(signup_user, standin, api_client):  # noqa: F811
    _, ws_id, sid = await _paid(signup_user, standin, plan="duo", period="yearly")
    now = int(time.time())
    await S._apply_event(_event("subscription.pending", sid, "pending", now + 5), _at(now + 5))
    approval = await lifecycle.open_approval_for_workspace(ws_id)
    assert approval and approval["amount"] == 1_899_000
    page = await api_client.get(f"/api/v1/billing/approval/{approval['token']}")
    assert page.status_code == 200 and page.json()["url"].startswith("https://rzp.io/i/") and page.json()["expired"] is False
    await S._apply_event(_event("subscription.charged", sid, "active", now + 10), _at(now + 10))
    page = await api_client.get(f"/api/v1/billing/approval/{approval['token']}")
    assert page.json()["url"] is None
    assert (await api_client.get("/api/v1/billing/approval/not-a-real-token")).status_code == 404


async def _charge(sid: str, ws_id: str, *, amount=99_900, method="card", **extra):
    now = int(time.time())
    payment = {"id": f"pay_{sid}_{now}", "amount": amount, "currency": "INR", "status": "captured", "method": method, "created_at": now, **extra}
    await S._apply_event(_event("subscription.charged", sid, "active", now, payment), _at(now))
    return payment


async def test_billing_shows_what_pays_the_plan_and_each_receipt(signup_user, standin):  # noqa: F811
    client, ws_id, sid = await _paid(signup_user, standin, plan="single")
    payment = await _charge(sid, ws_id, method="upi", vpa="asha@okbank")
    view = (await client.get("/api/v1/billing/subscription")).json()
    assert view["payment_method"] == {"kind": "upi", "label": "UPI Autopay · asha@okbank"}
    receipts = (await client.get("/api/v1/billing/invoices")).json()["invoices"]
    assert receipts and receipts[0]["status"] == "paid"
    one = await client.get(f"/api/v1/billing/invoices/{receipts[0]['id']}")
    assert one.status_code == 200 and one.json()["paid_with"] == "UPI Autopay · asha@okbank"
    # a refund Razorpay confirms shows on the receipt
    await S._apply_event({"event": "refund.processed", "payload": {"refund": {"entity": {"id": f"rfnd_{payment['id']}", "payment_id": payment["id"], "amount": payment["amount"], "status": "processed"}}}}, None)
    assert (await client.get(f"/api/v1/billing/invoices/{receipts[0]['id']}")).json()["status"] == "refunded"


async def test_a_new_payment_method_takes_over_at_the_renewal(signup_user, standin):  # noqa: F811
    client, _, sid = await _paid(signup_user, standin, plan="duo")
    res = await client.post("/api/v1/billing/payment-method")
    assert res.status_code == 200, res.text
    assert res.json()["terms"]["applies"] == "at_renewal" and res.json()["terms"]["plan"] == "duo"
    call = [kw for name, kw in standin.calls if name == "create_subscription"][-1]
    assert call["upfront"] is None and call["notes"]["replaces"]


async def test_ops_refunds_extends_grace_and_keeps_a_history(signup_user, standin, make_client):  # noqa: F811
    from app.db.mongo import users

    owner_client, ops_profile = await signup_user("Ops owner")
    await users.update_one({"id": ops_profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    _, ws_id, sid = await _paid(signup_user, standin, plan="single")
    payment = await _charge(sid, ws_id)

    too_much = await owner_client.post(f"/api/v1/ops/billing/workspace/{ws_id}/refund", json={"payment_id": payment["id"], "amount": 10_000_000, "reason": "Charged twice"})
    assert too_much.status_code == 400
    ok = await owner_client.post(f"/api/v1/ops/billing/workspace/{ws_id}/refund", json={"payment_id": payment["id"], "reason": "Charged twice"})
    assert ok.status_code == 200, ok.text
    assert ("refund", {"payment_id": payment["id"], "amount": None}) in standin.calls

    # no failed payment, so there is no grace to extend
    assert (await owner_client.post(f"/api/v1/ops/billing/workspace/{ws_id}/extend", json={"days": 7, "reason": "Bank outage"})).status_code == 400
    now = int(time.time())
    await S._apply_event(_event("subscription.pending", sid, "pending", now + 5), _at(now + 5))
    before = (await S.subs_col().find_one({"provider_subscription_id": sid}))["grace_until"]
    ext = await owner_client.post(f"/api/v1/ops/billing/workspace/{ws_id}/extend", json={"days": 7, "reason": "Bank outage"})
    assert ext.status_code == 200, ext.text
    after = (await S.subs_col().find_one({"provider_subscription_id": sid}))["grace_until"]
    assert (after - before).days == 7

    failed = (await owner_client.get("/api/v1/ops/billing/failed")).json()["failed"]
    assert any(r["workspace_id"] == ws_id and r["status"] == "retrying" for r in failed)
    history = (await owner_client.get("/api/v1/ops/billing/history")).json()["history"]
    assert {"refund", "extend_grace"} <= {h["action"] for h in history if h["target"] == ws_id}
    overview = (await owner_client.get("/api/v1/ops/billing/overview")).json()
    assert overview["paying_workspaces"] >= 1 and overview["founding_limit"] == 100

    # a member is not Ops
    member, _ = await signup_user("Member")
    assert (await member.post(f"/api/v1/ops/billing/workspace/{ws_id}/refund", json={"payment_id": payment["id"], "reason": "x y z"})).status_code in (401, 403)


async def test_a_new_workspace_can_be_covered_by_a_paid_plan_with_room(signup_user, standin):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="duo")
    entitlements.forget()
    cover = (await client.get("/api/v1/billing/coverage")).json()
    paid = [p for p in cover["plans"] if p["workspace_id"] == ws_id]
    assert paid and paid[0]["plan"] == "duo" and paid[0]["used"] == 1 and paid[0]["allowed"] == 5
    # the one free place is either open, or taken by a named workspace; never both
    assert cover["free_available"] == (cover["free_used_by"] is None)
    assert all(p["plan"] != "free" for p in cover["plans"])


async def test_the_reminder_goes_out_once_three_days_before_the_renewal(signup_user, standin, monkeypatch):  # noqa: F811
    sent: list[tuple[str, str]] = []

    async def _email(template, to, variables, *args, **kwargs):
        sent.append((template, to))

    from app.core import notifications

    monkeypatch.setattr(notifications, "send_templated_email", _email)
    _, ws_id, sid = await _paid(signup_user, standin, plan="single")
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    await S.subs_col().update_one({"provider_subscription_id": sid}, {"$set": {"charge_at": soon}})
    await lifecycle.reminder_tick()
    await lifecycle.reminder_tick()
    assert sum(1 for t, _ in sent if t == "billing-renewal-reminder") == 1
