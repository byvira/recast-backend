"""Payments safety: cancelling with a change waiting, a paid checkout never let go, late webhooks never moving a plan back, one worker
per webhook, partial refunds adding up, approval links after paying, founding places per workspace, a pause that ends the paid
period and holds scheduled posts, chained changes, and yearly plans carrying unused credits. Razorpay is the stand-in from
test_payments.py: real signature checks, no network."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.billing import credits, lifecycle, pause_holds
from app.billing import prices as PR
from app.billing import subscriptions as S
from app.db.mongo import content_pieces, workspaces
from tests.test_payments import CUSTOMER, _event, _owner_with_workspace, standin  # noqa: F401 - the fixture is used by name
from tests.test_payments_lifecycle import _at, _charge, _paid


async def _downgrade_waiting(client, standin):  # noqa: F811
    res = await client.post("/api/v1/billing/change", json={"plan": "single", "period": "monthly"})
    assert res.status_code == 200, res.text
    new_sid = res.json()["checkout"]["subscription_id"]
    now = int(time.time())
    standin.subs[new_sid]["status"] = "authenticated"
    await S._apply_event(_event("subscription.authenticated", new_sid, "authenticated", now + 1), _at(now + 1))
    return new_sid


async def test_cancelling_also_cancels_a_change_waiting_for_the_renewal(signup_user, standin):  # noqa: F811
    client, ws_id, sid = await _paid(signup_user, standin, plan="duo")
    new_sid = await _downgrade_waiting(client, standin)
    res = await client.post("/api/v1/billing/cancel", json={"reason": "not_using"})
    assert res.status_code == 200, res.text
    # the waiting change is cancelled at once, so nothing charges at the renewal; the plan in force ends at its renewal
    assert ("cancel_subscription", {"id": new_sid, "at_cycle_end": False}) in standin.calls
    assert (await S.subs_col().find_one({"provider_subscription_id": new_sid}))["status"] == "cancelled"
    assert (await S.subs_col().find_one({"provider_subscription_id": sid}))["cancel_at_period_end"] is True
    assert (await workspaces.find_one({"id": ws_id}))["plan"] == "duo"


async def test_pausing_is_refused_while_a_change_waits_for_the_renewal(signup_user, standin):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan="duo")
    await _downgrade_waiting(client, standin)
    res = await client.post("/api/v1/billing/pause", json={"months": 1})
    assert res.status_code == 400


async def test_a_second_change_cancels_the_first_one_that_has_not_started(signup_user, standin):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan="large", seats=12)
    first = await _downgrade_waiting(client, standin)
    res = await client.post("/api/v1/billing/change", json={"plan": "duo", "period": "monthly"})
    assert res.status_code == 200, res.text
    second = res.json()["checkout"]["subscription_id"]
    now = int(time.time())
    standin.subs[second]["status"] = "authenticated"
    await S._apply_event(_event("subscription.authenticated", second, "authenticated", now + 2), _at(now + 2))
    assert ("cancel_subscription", {"id": first, "at_cycle_end": False}) in standin.calls
    assert (await S.subs_col().find_one({"provider_subscription_id": first}))["status"] == "cancelled"


async def test_a_checkout_paid_after_all_is_never_let_go_by_a_second_one(signup_user, standin):  # noqa: F811
    client, _, ws_id = await _owner_with_workspace(signup_user)
    first = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "customer": CUSTOMER})
    sid = first.json()["checkout"]["subscription_id"]
    # paid in the window, but its message has not arrived
    standin.subs[sid]["status"] = "authenticated"
    again = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "customer": CUSTOMER})
    assert again.status_code == 409
    assert not any(name == "cancel_subscription" and kw["id"] == sid for name, kw in standin.calls)
    assert (await S.subs_col().find_one({"provider_subscription_id": sid}))["status"] == "authenticated"
    assert (await workspaces.find_one({"id": ws_id}))["plan"] == "single"


async def test_an_unpaid_checkout_is_still_let_go_by_a_second_one(signup_user, standin):  # noqa: F811
    client, _, _ = await _owner_with_workspace(signup_user)
    first = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "customer": CUSTOMER})
    sid = first.json()["checkout"]["subscription_id"]
    again = await client.post("/api/v1/billing/checkout", json={"plan": "single", "period": "monthly", "customer": CUSTOMER})
    assert again.status_code == 200, again.text
    assert (await S.subs_col().find_one({"provider_subscription_id": sid}))["status"] == "abandoned"


async def test_a_late_event_in_the_same_second_never_moves_a_plan_back(signup_user, standin):  # noqa: F811
    _, ws_id, sid = await _paid(signup_user, standin, plan="duo", period="yearly")
    now = int(time.time()) + 50
    await S._apply_event(_event("subscription.activated", sid, "active", now), _at(now))
    await S._apply_event(_event("subscription.authenticated", sid, "authenticated", now), _at(now))
    assert (await S.subs_col().find_one({"provider_subscription_id": sid}))["status"] == "active"
    assert (await workspaces.find_one({"id": ws_id}))["plan"] == "duo"


async def test_a_webhook_another_worker_is_applying_is_left_to_it(signup_user, standin):  # noqa: F811
    key = f"evt_{uuid4().hex}"
    await S.events_col().insert_one({
        "_id": key, "event": "subscription.activated", "body": {"event": "ping"}, "provider_created_at": None,
        "received_at": datetime.now(timezone.utc), "processed_at": None, "attempts": 1, "error": None, "outcome": None,
        "claimed_at": datetime.now(timezone.utc),
    })
    await S.process_event(key)
    row = await S.events_col().find_one({"_id": key})
    assert row["attempts"] == 1 and row["processed_at"] is None
    await S.events_col().update_one({"_id": key}, {"$set": {"claimed_at": datetime.now(timezone.utc) - timedelta(minutes=10)}})
    await S.process_event(key)
    assert (await S.events_col().find_one({"_id": key}))["processed_at"] is not None
    await S.events_col().delete_one({"_id": key})


async def test_partial_refunds_add_up_and_the_same_refund_counts_once(signup_user, standin):  # noqa: F811
    _, ws_id, sid = await _paid(signup_user, standin, plan="single")
    payment = await _charge(sid, ws_id, amount=100_000)

    def refund(rid: str, amount: int) -> dict:
        return {"event": "refund.processed", "payload": {"refund": {"entity": {"id": rid, "payment_id": payment["id"], "amount": amount, "status": "processed"}}}}

    await S._apply_event(refund("rfnd_a", 50_000), None)
    await S._apply_event(refund("rfnd_a", 50_000), None)
    pay = await S.payments_col().find_one({"id": payment["id"]})
    assert pay["refunded"] == 50_000 and pay["refund_status"] == "partly_refunded"
    await S._apply_event(refund("rfnd_b", 50_000), None)
    pay = await S.payments_col().find_one({"id": payment["id"]})
    assert pay["refunded"] == 100_000 and pay["refund_status"] == "refunded"


async def test_the_approval_page_says_approved_after_paying(signup_user, standin, api_client):  # noqa: F811
    _, ws_id, sid = await _paid(signup_user, standin, plan="duo", period="yearly")
    now = int(time.time())
    await S._apply_event(_event("subscription.pending", sid, "pending", now + 5), _at(now + 5))
    token = (await lifecycle.open_approval_for_workspace(ws_id))["token"]
    await S._apply_event(_event("subscription.charged", sid, "active", now + 10), _at(now + 10))
    page = (await api_client.get(f"/api/v1/billing/approval/{token}")).json()
    assert page["approved"] is True and page["expired"] is False


async def test_a_retried_renewal_keeps_one_approval_link(signup_user, standin):  # noqa: F811
    _, ws_id, sid = await _paid(signup_user, standin, plan="duo", period="yearly")
    now = int(time.time())
    await S._apply_event(_event("subscription.pending", sid, "pending", now + 5), _at(now + 5))
    sub = await S.subs_col().find_one({"provider_subscription_id": sid}, {"_id": 0})
    await lifecycle.open_approval({**sub, "charge_at": datetime.now(timezone.utc) + timedelta(days=1)}, standin)
    await lifecycle.open_approval({**sub, "charge_at": datetime.now(timezone.utc) + timedelta(days=2)}, standin)
    assert await lifecycle.approvals_col().count_documents({"subscription_id": sub["id"]}) == 1


async def test_a_workspace_holds_one_founding_place_however_many_subscriptions(signup_user, standin):  # noqa: F811
    client, _, _ = await _paid(signup_user, standin, plan="single")
    before = await S.founding_taken()
    res = await client.post("/api/v1/billing/change", json={"plan": "duo", "period": "monthly"})
    new_sid = res.json()["checkout"]["subscription_id"]
    now = int(time.time())
    await S._apply_event(_event("subscription.authenticated", new_sid, "authenticated", now + 1), _at(now + 1))
    assert await S.founding_taken() == before


async def test_a_paused_plan_ends_with_its_period_and_holds_scheduled_posts_until_it_is_back(signup_user, standin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pause_holds, "check_gate", lambda piece, honour_recorded_override=True: None)

    async def _no_blocker(piece, workspace_id):
        return None

    monkeypatch.setattr(pause_holds, "schedule_blocker", _no_blocker)
    client, ws_id, sid = await _paid(signup_user, standin, plan="single")
    later = datetime.now(timezone.utc) + timedelta(days=90)
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    for pid, when in (("later", later), ("soon", soon)):
        await content_pieces.insert_one({"piece_id": f"{pid}-{ws_id}", "workspace_id": ws_id, "platform": "linkedin", "publish_status": "queued",
                                         "publish_scheduled_at": when, "deleted": False})
    assert (await client.post("/api/v1/billing/pause", json={"months": 1})).status_code == 200
    sub = await S.subs_col().find_one({"provider_subscription_id": sid})
    await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"pause.pause_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    await lifecycle.pause_tick(standin)
    # still inside the paid period: the plan works and posts go out
    assert (await workspaces.find_one({"id": ws_id}))["plan"] == "single"
    assert await content_pieces.count_documents({"workspace_id": ws_id, "hold": {"$exists": True}}) == 0

    # the paid period ends: no message comes from the provider, the lifecycle job moves the workspace and holds its posts
    await S.subs_col().update_one({"id": sub["id"]}, {"$set": {"current_end": datetime.now(timezone.utc) - timedelta(minutes=1),
                                                               "pause.resume_at": datetime.now(timezone.utc) + timedelta(days=30)}})
    await lifecycle.pause_tick(standin)
    ws = await workspaces.find_one({"id": ws_id})
    assert ws["plan"] == "free" and ws["billing_status"] == "paused"
    assert await content_pieces.count_documents({"workspace_id": ws_id, "hold.reason": pause_holds.PLAN_PAUSED}) == 2

    # back: the post still ahead goes out as planned, the one whose time passed waits for a new time
    await content_pieces.update_one({"piece_id": f"soon-{ws_id}"}, {"$set": {"publish_scheduled_at": datetime.now(timezone.utc) - timedelta(hours=1)}})
    assert (await client.post("/api/v1/billing/resume")).status_code == 200
    later_doc = await content_pieces.find_one({"piece_id": f"later-{ws_id}"})
    soon_doc = await content_pieces.find_one({"piece_id": f"soon-{ws_id}"})
    assert "hold" not in later_doc and later_doc["publish_status"] == "queued"
    assert "hold" not in soon_doc and soon_doc["publish_status"] == "pending"
    assert (await workspaces.find_one({"id": ws_id}))["plan"] == "single"
    await content_pieces.delete_many({"workspace_id": ws_id})


def test_a_yearly_plan_bought_on_the_30th_renews_on_the_30th():
    q = PR.quote("single", "INR", "yearly", seats=1, founding=False, today=date(2026, 1, 30))
    assert q["renews_on"] == "2027-01-30"
    leap = PR.quote("single", "INR", "yearly", seats=1, founding=False, today=date(2028, 2, 29))
    assert leap["renews_on"] == "2029-02-28"


def test_unused_credits_carry_up_to_one_month_on_a_yearly_plan():
    assert credits.previous_period("2026-12") == "2026-11"
    assert credits.previous_period("2026-01") == "2025-12"
    assert credits.carry_amount({"yearly": True, "allowance": 1000, "used": 300}, 1000) == 700
    # never more than one month, even with credits carried and granted before
    assert credits.carry_amount({"yearly": True, "allowance": 1000, "carried": 900, "granted": 200, "used": 100}, 1000) == 1000
    assert credits.carry_amount({"yearly": True, "allowance": 1000, "used": 1200}, 1000) == 0
    # a month that was not on a yearly plan leaves nothing
    assert credits.carry_amount({"yearly": False, "allowance": 1000, "used": 0}, 1000) == 0
    assert credits.carry_amount(None, 1000) == 0


async def test_a_yearly_month_starts_with_last_months_unused_credits(signup_user, standin, monkeypatch):  # noqa: F811
    from app.core.config import settings
    from app.shared import entitlements

    _, ws_id, _ = await _paid(signup_user, standin, plan="single", period="yearly")
    # plans switched on only once the workspace exists, as signing up is not what is tested here
    monkeypatch.setattr(settings, "BILLING_ENFORCED", True)
    entitlements.forget()
    credits.forget()
    pool = await credits.pool_for(ws_id)
    assert pool.yearly and pool.allowance
    period = pool.period()
    prev = credits.previous_period(period)
    await credits.balances_col().delete_many({"pool_id": pool.id})
    await credits.balances_col().insert_one({
        "_id": credits._balance_id(pool.id, prev), "pool_id": pool.id, "period": prev, "allowance": pool.allowance, "used": 100,
        "reserved": 0, "granted": 0, "carried": 0, "yearly": True,
    })
    credits._ensured.clear()
    await credits._ensure_balance(pool, period)
    await credits._ensure_balance(pool, period)
    doc = await credits.balances_col().find_one({"_id": credits._balance_id(pool.id, period)})
    assert doc["carried"] == min(pool.allowance, pool.allowance - 100)
    assert await credits.ledger_col().count_documents({"pool_id": pool.id, "kind": "carry"}) == 1
    summary = await credits.summary(ws_id)
    assert summary["carried"] == doc["carried"] and summary["limit"] == pool.allowance + doc["carried"]
    assert not [d for d in await credits.reconcile(period) if d["pool_id"] == pool.id]
    await credits.balances_col().delete_many({"pool_id": pool.id})
    await credits.ledger_col().delete_many({"pool_id": pool.id})
    entitlements.forget()
    credits.forget()


async def test_a_workspace_with_a_renewing_plan_is_not_deleted_until_the_plan_is_cancelled(signup_user, standin):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    refused = await client.delete(f"/api/v1/workspaces/{ws_id}")
    assert refused.status_code == 409
    assert "deleted_at" not in await workspaces.find_one({"id": ws_id})
    assert (await client.post("/api/v1/billing/cancel", json={})).status_code == 200
    assert (await client.delete(f"/api/v1/workspaces/{ws_id}")).status_code == 200


async def test_a_plan_paused_past_its_period_can_be_cancelled_and_never_resumes(signup_user, standin):  # noqa: F811
    client, ws_id, sid = await _paid(signup_user, standin, plan="single")
    standin.subs[sid]["status"] = "paused"
    await S.subs_col().update_one({"provider_subscription_id": sid}, {"$set": {
        "status": "paused", "current_end": datetime.now(timezone.utc) - timedelta(days=1),
        "pause": {"months": 1, "resume_at": datetime.now(timezone.utc) + timedelta(days=20)},
    }})
    await S.apply_for_workspace(ws_id)
    assert (await workspaces.find_one({"id": ws_id}))["billing_status"] == "paused"
    res = await client.post("/api/v1/billing/cancel", json={})
    assert res.status_code == 200, res.text
    assert ("cancel_subscription", {"id": sid, "at_cycle_end": False}) in standin.calls
    sub = await S.subs_col().find_one({"provider_subscription_id": sid})
    assert sub["status"] == "cancelled" and sub["pause"] is None
    assert (await workspaces.find_one({"id": ws_id}))["billing_status"] == "ended"


async def test_checkouts_take_the_founding_turn_one_at_a_time(standin):  # noqa: F811
    import asyncio

    order: list[str] = []

    async def checkout(name: str) -> None:
        async with S.founding_turn():
            order.append(f"{name} in")
            await asyncio.sleep(0.3)
            order.append(f"{name} out")

    await asyncio.gather(checkout("a"), checkout("b"))
    # never two inside at once
    assert order in (["a in", "a out", "b in", "b out"], ["b in", "b out", "a in", "a out"])
    assert await S.locks_col().count_documents({"_id": "founding"}) == 0


async def test_a_turn_left_by_a_crash_runs_out(standin):  # noqa: F811
    await S.locks_col().update_one({"_id": "founding"}, {"$set": {"holder": "crashed", "until": datetime.now(timezone.utc) - timedelta(seconds=1)}}, upsert=True)
    async with S.founding_turn():
        pass
    assert await S.locks_col().count_documents({"_id": "founding"}) == 0


async def test_a_disputed_payment_is_raised_until_the_dispute_closes(signup_user, standin):  # noqa: F811
    from app.billing import watch

    _, ws_id, sid = await _paid(signup_user, standin, plan="single")
    payment = await _charge(sid, ws_id)
    did = f"disp_{uuid4().hex[:10]}"
    respond_by = int(time.time()) + 5 * 86400

    def dispute(event: str, status: str) -> dict:
        return {"event": event, "payload": {"dispute": {"entity": {
            "id": did, "payment_id": payment["id"], "amount": payment["amount"], "currency": "INR", "status": status,
            "reason_code": "fraud", "reason_description": "Customer says they did not pay", "respond_by": respond_by,
        }}}}

    assert await S._apply_event(dispute("payment.dispute.created", "open"), None) == "applied"
    found = await watch.run_checks(standin)
    mine = [f for f in found if f.key == f"dispute:{did}"]
    assert mine and mine[0].severity == "critical" and mine[0].workspace_id == ws_id
    await S._apply_event(dispute("payment.dispute.won", "won"), None)
    found = await watch.run_checks(standin)
    assert not [f for f in found if f.key == f"dispute:{did}"]
    await S.disputes_col().delete_many({"id": did})
