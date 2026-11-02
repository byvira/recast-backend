"""Each paid plan end to end: Single, Duo and Large (a small team and one past the lower seat price), monthly and yearly. What checkout
charges, the plan and credits the workspace gets, which renewals need the bank's approval, and every move between plans and seats.
Razorpay is the stand-in from test_payments.py: real signature checks, no network."""
from __future__ import annotations

import time

import pytest

from app.billing import lifecycle
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.db.mongo import workspaces
from app.shared import entitlements
from tests.test_payments import CUSTOMER, _event, _owner_with_workspace, standin  # noqa: F401 - the fixture is used by name
from tests.test_payments_lifecycle import _at, _paid

PLANS = [("single", 1), ("duo", 2), ("large", 3), ("large", 30)]
CREDITS = {"single": lambda n: 1200, "duo": lambda n: 2500, "large": lambda n: 1000 * n}


@pytest.mark.parametrize("period", ["monthly", "yearly"])
@pytest.mark.parametrize("plan,seats", PLANS)
async def test_each_plan_charges_what_was_quoted_and_gives_its_plan_and_credits(signup_user, standin, plan, seats, period):  # noqa: F811
    client, _, ws_id = await _owner_with_workspace(signup_user)
    q = (await client.post("/api/v1/billing/quote", json={"plan": plan, "period": period, "seats": seats, "billing_day": 1, "country": "IN"})).json()
    res = await client.post("/api/v1/billing/checkout", json={"plan": plan, "period": period, "seats": seats, "billing_day": 1, "customer": CUSTOMER})
    assert res.status_code == 200, res.text
    sub = await S.subs_col().find_one({"provider_subscription_id": res.json()["checkout"]["subscription_id"]})
    # the renewal amount is the price table's, with the founding price while places are left
    assert sub["amount"] == PR.total(plan, "INR", period, seats=seats, founding=sub["founding"]) == q["renew"]
    assert sub["seats"] == seats and sub["first_amount"] == q["today"]
    call = [kw for name, kw in standin.calls if name == "create_subscription"][-1]
    assert call["quantity"] == (seats if plan == "large" else 1)
    if period == "monthly":
        # the days until the 1st are taken up front with the mandate
        assert call["start_at"] and call["upfront"]["amount"] == q["today"]
    else:
        assert call["start_at"] is None and call["upfront"] is None and q["today"] == q["renew"]

    sid = sub["provider_subscription_id"]
    now = int(time.time())
    standin.subs[sid]["status"] = "active"
    await S._apply_event(_event("subscription.activated", sid, "active", now), _at(now))
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == plan and ws["plan_seats"] == seats and ws["billing_period"] == period
    entitlements.forget()
    ent = await entitlements.entitlements_for(ws_id)
    assert ent.plan_limits["credits_month"] == CREDITS[plan](seats)
    assert ent.plan_limits["seats"] == seats


@pytest.mark.parametrize("plan,seats,period", [
    ("single", 1, "monthly"), ("single", 1, "yearly"), ("duo", 2, "monthly"), ("duo", 2, "yearly"),
    ("large", 3, "monthly"), ("large", 20, "monthly"), ("large", 3, "yearly"),
])
async def test_only_renewals_over_the_bank_limit_ask_for_approval(signup_user, standin, plan, seats, period):  # noqa: F811
    _, ws_id, sid = await _paid(signup_user, standin, plan=plan, seats=seats, period=period)
    sub = await S.subs_col().find_one({"provider_subscription_id": sid})
    now = int(time.time())
    await S._apply_event(_event("subscription.pending", sid, "pending", now + 5), _at(now + 5))
    approval = await lifecycle.open_approval_for_workspace(ws_id)
    assert bool(approval) == (sub["amount"] > S.AUTO_DEBIT_LIMIT)


# (from plan, seats) -> (to plan, seats): applies now or at the renewal, and whether anything is paid today
MOVES = [
    (("single", 1), ("duo", 2), "now", True),
    (("single", 1), ("large", 3), "now", True),
    (("duo", 2), ("large", 3), "now", True),
    (("duo", 2), ("single", 1), "at_renewal", False),
    (("large", 3), ("duo", 2), "at_renewal", False),
    (("large", 3), ("single", 1), "at_renewal", False),
    (("large", 3), ("large", 5), "now", True),
    (("large", 5), ("large", 3), "at_renewal", False),
]


@pytest.mark.parametrize("start,end,applies,pays", MOVES)
async def test_every_move_between_plans_applies_when_it_should(signup_user, standin, monkeypatch, start, end, applies, pays):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan=start[0], seats=start[1])
    q = await client.post("/api/v1/billing/change/quote", json={"plan": end[0], "period": "monthly", "seats": end[1]})
    assert q.status_code == 200, q.text
    assert q.json()["applies"] == applies
    assert (q.json()["today"] > 0) == pays


async def test_large_seats_follow_what_is_bought_not_the_lower_price_from_25(signup_user, standin):  # noqa: F811
    # without the founding price, 26 seats cost less than 24: adding seats still applies now, removing them still waits
    client, _, sid = await _paid(signup_user, standin, plan="large", seats=24)
    await S.subs_col().update_one({"provider_subscription_id": sid}, {"$set": {"founding": False, "amount": PR.total("large", "INR", "monthly", seats=24)}})
    up = (await client.post("/api/v1/billing/change/quote", json={"plan": "large", "period": "monthly", "seats": 25})).json()
    assert up["applies"] == "now" and up["today"] == 0 and up["renew"] == PR.total("large", "INR", "monthly", seats=25)
    client2, _, sid2 = await _paid(signup_user, standin, plan="large", seats=26)
    await S.subs_col().update_one({"provider_subscription_id": sid2}, {"$set": {"founding": False, "amount": PR.total("large", "INR", "monthly", seats=26)}})
    down = (await client2.post("/api/v1/billing/change/quote", json={"plan": "large", "period": "monthly", "seats": 24})).json()
    assert down["applies"] == "at_renewal" and down["today"] == 0


@pytest.mark.parametrize("start", [("single", 1), ("large", 3)])
async def test_switching_between_monthly_and_yearly_waits_for_the_renewal(signup_user, standin, start):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan=start[0], seats=start[1])
    q = (await client.post("/api/v1/billing/change/quote", json={"plan": start[0], "period": "yearly", "seats": start[1]})).json()
    assert q["applies"] == "at_renewal" and q["today"] == 0


@pytest.mark.parametrize("to_plan,people,ok", [("duo", 2, True), ("duo", 3, False), ("single", 2, False), ("single", 1, True)])
async def test_a_smaller_plan_needs_room_for_the_people_already_on_it(signup_user, standin, monkeypatch, to_plan, people, ok):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan="large", seats=3)
    real = entitlements.count_usage

    async def usage(ent, key):
        return people if key == "seats" else await real(ent, key)

    monkeypatch.setattr(entitlements, "count_usage", usage)
    q = await client.post("/api/v1/billing/change/quote", json={"plan": to_plan, "period": "monthly", "seats": 1})
    c = await client.post("/api/v1/billing/change", json={"plan": to_plan, "period": "monthly", "seats": 1})
    assert (q.status_code == 200) == ok and (c.status_code == 200) == ok
    if not ok:
        assert "Remove people first" in q.json()["detail"]["message"]


async def test_a_smaller_plan_needs_room_for_the_workspaces_on_it(signup_user, standin, monkeypatch):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan="large", seats=3)
    real = entitlements.count_usage

    async def usage(ent, key):
        return 8 if key == "owned_workspaces" else 1 if key == "seats" else await real(ent, key)

    monkeypatch.setattr(entitlements, "count_usage", usage)
    q = await client.post("/api/v1/billing/change/quote", json={"plan": "duo", "period": "monthly", "seats": 2})
    assert q.status_code == 400 and "covers 8 workspaces" in q.json()["detail"]["message"]
