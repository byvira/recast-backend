"""Credit packs, prices edited from Ops, and the receipt email: who may buy, paying once, credits added once, spent after the month's
own credits, given back at the month's end, ended after 12 months, with the plan or by a refund; prices that change without a code
change; one email per receipt. Razorpay is the stand-in from test_payments.py: real signature checks, no network."""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.billing import credits, topups
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.db.mongo import get_db, users, workspaces
from app.shared import entitlements
from tests.test_payments import CUSTOMER, KEY_SECRET, _event, _owner_with_workspace, standin  # noqa: F401 - the fixture is used by name
from tests.test_payments_lifecycle import _at, _charge, _paid


def _sig(order_id: str, payment_id: str) -> str:
    return hmac.new(KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


async def _buy(client, standin, pack_id="credits-500", status="captured"):  # noqa: F811
    res = await client.post("/api/v1/billing/topup", json={"pack_id": pack_id})
    assert res.status_code == 200, res.text
    order_id = res.json()["checkout"]["order_id"]
    payment = standin.pay_order(order_id, status=status)
    done = await client.post("/api/v1/billing/topup/confirm", json={
        "razorpay_payment_id": payment["id"], "razorpay_order_id": order_id, "razorpay_signature": _sig(order_id, payment["id"]),
    })
    return res.json(), payment, done


def _plans_on(monkeypatch) -> None:
    """Plans switched on once the workspace exists: signing up is not what these tests check."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "BILLING_ENFORCED", True)
    entitlements.forget()
    credits.forget()


async def test_only_a_paid_plan_can_buy_a_pack(signup_user, standin):  # noqa: F811
    client, _, _ = await _owner_with_workspace(signup_user)
    offer = (await client.get("/api/v1/billing/topups")).json()
    assert offer["can_buy"] is False and "paid plans" in offer["reason"]
    assert [p["id"] for p in offer["packs"]] == ["credits-500", "credits-1000", "credits-2500"]
    assert (await client.post("/api/v1/billing/topup", json={"pack_id": "credits-500"})).status_code == 403


async def test_a_pack_is_paid_once_and_its_credits_are_added_once(signup_user, standin, monkeypatch):  # noqa: F811
    sent: list[str] = []

    async def _email(template, to, variables):
        sent.append(template)
        return True

    from app.core import notifications

    monkeypatch.setattr(notifications, "send_templated_email", _email)
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    assert (await client.get("/api/v1/billing/topups")).json()["can_buy"] is True
    opened, payment, done = await _buy(client, standin)
    assert done.status_code == 200 and done.json()["topup"]["status"] == "paid"
    assert [kw for name, kw in standin.calls if name == "create_order"][-1]["amount"] == 24_900
    pool = await credits.pool_for(ws_id)
    assert (await credits.bought_left(pool.id))[0] == 500
    # the provider tells it again, twice: nothing more is added
    for _ in range(2):
        await S._apply_event({"event": "payment.captured", "payload": {"payment": {"entity": payment}}}, None)
    assert (await credits.bought_left(pool.id))[0] == 500
    assert await credits.ledger_col().count_documents({"pool_id": pool.id, "kind": "purchase"}) == 1
    receipts = [r async for r in get_db()["billing_invoices"].find({"payment_id": payment["id"]})]
    assert len(receipts) == 1 and receipts[0]["lines"][0]["label"] == "Ekavo credits, 500"
    assert sent.count("billing-receipt") == 1
    offer = (await client.get("/api/v1/billing/topups")).json()
    assert offer["bought_left"] == 500 and offer["bought_until"]


async def test_a_forged_confirm_adds_nothing(signup_user, standin):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    res = await client.post("/api/v1/billing/topup", json={"pack_id": "credits-500"})
    order_id = res.json()["checkout"]["order_id"]
    payment = standin.pay_order(order_id)
    bad = await client.post("/api/v1/billing/topup/confirm", json={
        "razorpay_payment_id": payment["id"], "razorpay_order_id": order_id, "razorpay_signature": "forged",
    })
    assert bad.status_code == 400
    assert (await credits.bought_left((await credits.pool_for(ws_id)).id))[0] == 0


async def test_an_authorised_payment_is_captured_then_added(signup_user, standin):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="duo")
    _, payment, done = await _buy(client, standin, pack_id="credits-1000", status="authorized")
    assert ("capture_payment", {"id": payment["id"], "amount": 44_900}) in standin.calls
    assert done.json()["topup"]["status"] == "paid"
    assert (await credits.bought_left((await credits.pool_for(ws_id)).id))[0] == 1000


async def test_a_payment_of_the_wrong_amount_adds_nothing(signup_user, standin):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    res = await client.post("/api/v1/billing/topup", json={"pack_id": "credits-500"})
    order_id = res.json()["checkout"]["order_id"]
    payment = {**standin.pay_order(order_id), "amount": 100}
    assert await topups.apply_payment(payment) == "mismatch"
    assert (await credits.bought_left((await credits.pool_for(ws_id)).id))[0] == 0


async def test_bought_credits_are_spent_after_the_months_own_and_the_rest_waits_for_next_month(signup_user, standin, monkeypatch):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    await _buy(client, standin)
    _plans_on(monkeypatch)
    pool = await credits.pool_for(ws_id)
    period = pool.period()
    await credits._ensure_balance(pool, period)
    bid = credits._balance_id(pool.id, period)
    # the month's own credits are used up
    await credits.balances_col().update_one({"_id": bid}, {"$set": {"used": pool.allowance}})
    credits.forget()
    hold = await credits.reserve(ws_id, None, "text_post")
    assert hold.credits > 0
    left, _ = await credits.bought_left(pool.id)
    assert left == 500 - hold.credits
    await credits.release(hold)
    doc = await credits.balances_col().find_one({"_id": bid})
    assert doc["bought_in"] == hold.credits
    # the members' history shows the purchase, not the moves between the pack and the month
    kinds = {r["kind"] for r in (await credits.ledger(ws_id))["items"]}
    assert "purchase" in kinds and "bought_in" not in kinds

    # next month: the credits moved in but not used go back to the pack
    nxt = credits.period_of(credits.resets_at(period) + timedelta(hours=1), pool.billing_day)
    await credits._return_bought(pool.id, nxt)
    assert (await credits.bought_left(pool.id))[0] == 500
    assert (await credits.balances_col().find_one({"_id": bid}))["bought_in"] == 0
    assert not await credits.reconcile_wallets() or all(d["pool_id"] != pool.id for d in await credits.reconcile_wallets())
    await credits.balances_col().delete_many({"pool_id": pool.id})


async def test_the_owners_own_limit_is_never_passed_by_bought_credits(signup_user, standin, monkeypatch):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    await _buy(client, standin)
    _plans_on(monkeypatch)
    pool = await credits.pool_for(ws_id)
    await credits.set_owner_limit(ws_id, 10)
    await credits._ensure_balance(pool, pool.period())
    await credits.balances_col().update_one({"_id": credits._balance_id(pool.id, pool.period())}, {"$set": {"used": 10}})
    credits.forget()
    with pytest.raises(credits.CreditsExhausted):
        await credits.reserve(ws_id, None, "text_post")
    assert (await credits.bought_left(pool.id))[0] == 500
    await credits.set_owner_limit(ws_id, None)
    await credits.balances_col().delete_many({"pool_id": pool.id})


async def test_bought_credits_end_after_12_months(signup_user, standin):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    await _buy(client, standin)
    pool = await credits.pool_for(ws_id)
    await credits.wallets_col().update_one({"_id": pool.id}, {"$set": {"lots.0.expires_at": datetime.now(timezone.utc) - timedelta(days=1)}})
    await credits.expire_bought()
    assert (await credits.bought_left(pool.id))[0] == 0
    assert await credits.ledger_col().count_documents({"pool_id": pool.id, "kind": "expire"}) == 1


async def test_bought_credits_end_with_the_paid_plan(signup_user, standin):  # noqa: F811
    client, ws_id, sid = await _paid(signup_user, standin, plan="single")
    await _buy(client, standin)
    now = int(time.time())
    await S._apply_event(_event("subscription.cancelled", sid, "cancelled", now + 30), _at(now + 30))
    assert (await workspaces.find_one({"id": ws_id}))["billing_status"] == "ended"
    assert (await credits.bought_left((await credits.pool_for(ws_id)).id))[0] == 0


async def test_a_refunded_pack_ends_its_unused_credits(signup_user, standin):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    _, payment, _ = await _buy(client, standin)
    pool = await credits.pool_for(ws_id)
    half = {"id": "rfnd_half", "payment_id": payment["id"], "amount": payment["amount"] // 2, "status": "processed"}
    await S._apply_event({"event": "refund.processed", "payload": {"refund": {"entity": half}}}, None)
    assert (await credits.bought_left(pool.id))[0] == 250
    rest = {"id": "rfnd_rest", "payment_id": payment["id"], "amount": payment["amount"] - payment["amount"] // 2, "status": "processed"}
    await S._apply_event({"event": "refund.processed", "payload": {"refund": {"entity": rest}}}, None)
    assert (await credits.bought_left(pool.id))[0] == 0
    assert await credits.ledger_col().count_documents({"pool_id": pool.id, "kind": "clawback"}) == 2


async def test_ops_changes_prices_without_a_code_change(signup_user, standin):  # noqa: F811
    owner_client, ops_profile = await signup_user("Ops owner")
    await users.update_one({"id": ops_profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    try:
        book = (await owner_client.get("/api/v1/ops/billing/prices")).json()["book"]
        book["monthly"]["single"]["INR"] = 109_900
        book["packs"].append({"id": "credits-5000", "credits": 5000, "prices": {"INR": 179_900}, "active": True})
        res = await owner_client.put("/api/v1/ops/billing/prices", json={"book": book, "reason": "New prices for launch"})
        assert res.status_code == 200, res.text
        # a founding price above the usual one is refused, with the reason
        bad = {**book, "founding": {**book["founding"], "single": {"INR": 200_000, "USD": 1_500}}}
        refused = await owner_client.put("/api/v1/ops/billing/prices", json={"book": bad, "reason": "Typo"})
        assert refused.status_code == 400 and "founding price" in refused.json()["detail"]

        client, _, _ = await _owner_with_workspace(signup_user)
        plans = (await client.get("/api/v1/billing/plans")).json()
        single = next(r for r in plans["list_prices"]["INR"] if r["plan"] == "single")
        assert single["monthly"]["amount"] == 109_900
        assert "credits-5000" in [p["id"] for p in (await client.get("/api/v1/billing/topups")).json()["packs"]]
        history = (await owner_client.get("/api/v1/ops/billing/history")).json()["history"]
        assert any(h["action"] == "prices_changed" and h["reason"] == "New prices for launch" for h in history)
        # members cannot change prices
        assert (await client.put("/api/v1/ops/billing/prices", json={"book": book, "reason": "x y z"})).status_code in (401, 403)
    finally:
        await get_db()["billing_settings"].delete_one({"_id": "prices"})
        await PR.load_book(force=True)


async def test_a_running_subscription_keeps_its_price_after_a_change_in_ops(signup_user, standin):  # noqa: F811
    _, ws_id, sid = await _paid(signup_user, standin, plan="single")
    before = (await S.subs_col().find_one({"provider_subscription_id": sid}))["amount"]
    try:
        book = await PR.load_book(force=True)
        changed = {**book, "monthly": {**book["monthly"], "single": {"INR": 149_900, "USD": 2_500}}}
        await PR.save_book(changed, by="test", reason="Price rise")
        payment = await _charge(sid, ws_id, amount=before)
        assert (await S.subs_col().find_one({"provider_subscription_id": sid}))["amount"] == before
        assert payment["amount"] == before
    finally:
        await get_db()["billing_settings"].delete_one({"_id": "prices"})
        await PR.load_book(force=True)


def test_a_price_book_with_mistakes_is_refused():
    base = PR.DEFAULT_BOOK
    for broken, words in (
        ({**base, "large_volume": {"INR": 99_000, "USD": 1_300}}, "lower Large seat price"),
        ({**base, "yearly_months": 13}, "1 to 12 months"),
        ({**base, "packs": [{"id": "a", "credits": 10, "prices": {"INR": 1000}}]}, "short name"),
        ({**base, "packs": [{"id": "pack-x", "credits": 0, "prices": {"INR": 1000}}]}, "credits must be"),
        ({**base, "packs": [{"id": "pack-x", "credits": 10, "prices": {"INR": 50}}]}, "at least"),
    ):
        with pytest.raises(ValueError, match=words):
            PR.validate_book(broken)


def test_a_typed_amount_is_priced_by_its_band_and_fewer_credits_never_cost_more():
    bands = PR.DEFAULT_BOOK["custom_credits"]["bands"]
    price = {n: PR.custom_price_raw(n, bands) for n in (100, 999, 1_000, 2_499, 2_500, 3_001)}
    assert price[100] == 5_000 and price[1_000] == 45_000 and price[2_500] == 100_000
    assert price[999] <= price[1_000] and price[2_499] <= price[2_500]
    assert price[3_001] == 120_100   # rounded up to a whole rupee
    for broken, words in (
        ({"on": True, "min": 100, "max": 50, "bands": bands}, "smallest first"),
        ({"on": True, "min": 100, "max": 500, "bands": [{"from": 10, "per_1000": 50_000}]}, "starts at 1 credit"),
        ({"on": True, "min": 100, "max": 500, "bands": [{"from": 1, "per_1000": 40_000}, {"from": 50, "per_1000": 50_000}]}, "cost more"),
    ):
        with pytest.raises(ValueError, match=words):
            PR.validate_book({**PR.DEFAULT_BOOK, "custom_credits": broken})


async def test_a_member_buys_the_number_of_credits_they_type(signup_user, standin):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    offer = (await client.get("/api/v1/billing/topups")).json()
    assert offer["custom"]["min"] == 100 and offer["custom"]["max"] == 50_000
    quote = (await client.get("/api/v1/billing/topup/quote", params={"credits": 999})).json()
    assert quote["amount"] == 45_000 and quote["next"] == {"credits": 1_000, "amount": 45_000, "extra": 0}
    assert (await client.get("/api/v1/billing/topup/quote", params={"credits": 50})).status_code == 400
    assert (await client.post("/api/v1/billing/topup", json={"credits": 60_000})).status_code == 400
    assert (await client.post("/api/v1/billing/topup", json={"pack_id": "credits-500", "credits": 700})).status_code == 422
    res = await client.post("/api/v1/billing/topup", json={"credits": 700})
    assert res.status_code == 200, res.text
    assert res.json()["topup"]["amount"] == 35_000 and res.json()["topup"]["credits"] == 700
    order_id = res.json()["checkout"]["order_id"]
    payment = standin.pay_order(order_id, status="captured")
    done = await client.post("/api/v1/billing/topup/confirm", json={
        "razorpay_payment_id": payment["id"], "razorpay_order_id": order_id, "razorpay_signature": _sig(order_id, payment["id"]),
    })
    assert done.status_code == 200 and done.json()["topup"]["status"] == "paid"
    pool = await credits.pool_for(ws_id)
    assert (await credits.bought_left(pool.id))[0] == 700
