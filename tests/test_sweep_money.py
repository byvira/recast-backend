"""Faults found by the billing sweep: a paid pack whose credits never arrive when the server stops half way."""
from __future__ import annotations

import pytest

from app.billing import credits, topups
from tests.test_payments import standin  # noqa: F401 - the fixture is used by name
from tests.test_payments_lifecycle import _paid


async def test_credits_missing_after_a_stop_between_the_history_and_the_wallet_are_added_when_the_purchase_is_told_again(signup_user, standin, monkeypatch):  # noqa: F811
    _, ws_id, _ = await _paid(signup_user, standin, plan="single")
    pool = await credits.pool_for(ws_id)
    real = credits.wallets_col
    calls = {"n": 0}

    class Stops:
        def __init__(self, col):
            self._col = col

        async def update_one(self, *a, **k):
            calls["n"] += 1
            raise RuntimeError("the server stopped here")

        def __getattr__(self, name):
            return getattr(self._col, name)

    monkeypatch.setattr(credits, "wallets_col", lambda: Stops(real()))
    with pytest.raises(RuntimeError):
        await credits.add_bought(ws_id, topup_id="pack-sweep-1", credits=500, label="500 credits")
    monkeypatch.setattr(credits, "wallets_col", real)

    assert (await credits.bought_left(pool.id))[0] == 0, "the stop left the wallet empty"
    added = await credits.add_bought(ws_id, topup_id="pack-sweep-1", credits=500, label="500 credits")
    assert added is True, "telling it again must finish the job"
    assert (await credits.bought_left(pool.id))[0] == 500
    assert await credits.ledger_col().count_documents({"pool_id": pool.id, "kind": "purchase"}) == 1, "one history row, not two"
    assert await credits.add_bought(ws_id, topup_id="pack-sweep-1", credits=500, label="500 credits") is False, "and a third time adds nothing"
    assert (await credits.bought_left(pool.id))[0] == 500


async def test_a_paid_pack_whose_credits_were_never_added_gets_them_when_the_provider_tells_it_again(signup_user, standin, monkeypatch):  # noqa: F811
    client, ws_id, _ = await _paid(signup_user, standin, plan="single")
    pool = await credits.pool_for(ws_id)
    order = (await client.post("/api/v1/billing/topup", json={"pack_id": "credits-500"})).json()["checkout"]["order_id"]
    payment = standin.pay_order(order, status="captured")

    real = credits.add_bought
    state = {"stopped": False}

    async def stops_once(*a, **k):
        if not state["stopped"]:
            state["stopped"] = True
            raise RuntimeError("the server stopped after marking the pack paid")
        return await real(*a, **k)

    monkeypatch.setattr(credits, "add_bought", stops_once)
    with pytest.raises(RuntimeError):
        await topups.apply_payment(payment)
    assert (await credits.bought_left(pool.id))[0] == 0, "paid, and no credits yet"

    again = await topups.apply_payment(payment)
    assert again == "applied", f"the repeat message answered {again!r} and the customer was left without their credits"
    assert (await credits.bought_left(pool.id))[0] == 500
    assert await topups.apply_payment(payment) == "already", "after that it really is done"
    assert (await credits.bought_left(pool.id))[0] == 500
    assert await credits.ledger_col().count_documents({"pool_id": pool.id, "kind": "purchase"}) == 1


async def test_running_out_of_credits_on_a_campaign_picture_is_said_not_hidden_as_a_failure(monkeypatch):
    from unittest.mock import AsyncMock

    from app.pipelines.campaigns import media as campaign_media

    async def out_of_credits(*a, **k):
        raise credits._limited_refusal()

    monkeypatch.setattr(campaign_media, "_context", AsyncMock(return_value=object()))
    monkeypatch.setattr(campaign_media, "_image_request", AsyncMock(return_value=object()))
    monkeypatch.setattr("app.api.v1.image_assets.create_image_asset", out_of_credits)
    with pytest.raises(credits.CreditsExhausted):
        await campaign_media._regenerate_image({"id": "c-sweep"}, {}, "p-sweep", workspace_id="w-sweep", user_id="u-sweep")


async def test_any_other_failure_on_a_campaign_picture_still_never_fails_the_post(monkeypatch):
    from unittest.mock import AsyncMock

    from app.pipelines.campaigns import media as campaign_media

    async def broken(*a, **k):
        raise RuntimeError("the picture service is down")

    monkeypatch.setattr(campaign_media, "_context", AsyncMock(return_value=object()))
    monkeypatch.setattr(campaign_media, "_image_request", AsyncMock(return_value=object()))
    monkeypatch.setattr(campaign_media, "_record_status", AsyncMock())
    monkeypatch.setattr("app.api.v1.image_assets.create_image_asset", broken)
    result = await campaign_media._regenerate_image({"id": "c-sweep"}, {}, "p-sweep", workspace_id="w-sweep", user_id="u-sweep")
    assert result["state"] == "failed"


async def test_two_refund_messages_at_the_same_moment_take_back_the_credits_once(signup_user, standin):  # noqa: F811
    import asyncio
    from datetime import datetime, timezone

    _, ws_id, _ = await _paid(signup_user, standin, plan="single")
    pool = await credits.pool_for(ws_id)
    topup = {"id": "pack-refund-sweep", "workspace_id": ws_id, "pool_id": pool.id, "payment_id": "pay_refund_sweep", "amount": 24_900, "currency": "INR",
             "credits": 500, "status": "paid", "created_at": datetime.now(timezone.utc)}
    await topups.topups_col().delete_many({"id": topup["id"]})
    await topups.topups_col().insert_one(dict(topup))
    await credits.add_bought(ws_id, topup_id=topup["id"], credits=500, label="500 credits")
    assert (await credits.bought_left(pool.id))[0] == 500
    try:
        half = 24_900 // 2
        await asyncio.gather(topups.on_refund("pay_refund_sweep", half), topups.on_refund("pay_refund_sweep", half))
        left = (await credits.bought_left(pool.id))[0]
        assert left == 500 - round(500 * half / 24_900), f"{500 - left} credits were taken back for a refund worth {round(500 * half / 24_900)}"
        await topups.on_refund("pay_refund_sweep", 24_900)
        assert (await credits.bought_left(pool.id))[0] == 0, "a later, larger refund takes back the rest and no more"
    finally:
        await topups.topups_col().delete_many({"id": topup["id"]})


async def test_the_nightly_check_finishes_a_paid_pack_whose_credits_never_arrived_and_leaves_the_rest_alone(signup_user, standin):  # noqa: F811
    from datetime import datetime, timedelta, timezone

    _, ws_id, _ = await _paid(signup_user, standin, plan="single")
    pool = await credits.pool_for(ws_id)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    base = {"workspace_id": ws_id, "pool_id": pool.id, "amount": 24_900, "currency": "INR", "credits": 500, "status": "paid", "paid_at": old, "created_at": old}
    lost = {**base, "id": "pack-night-lost", "payment_id": "pay_night_lost"}
    fine = {**base, "id": "pack-night-fine", "payment_id": "pay_night_fine", "credits": 100}
    fresh = {**base, "id": "pack-night-fresh", "payment_id": "pay_night_fresh", "paid_at": datetime.now(timezone.utc)}
    ids = [lost["id"], fine["id"], fresh["id"]]
    await topups.topups_col().delete_many({"id": {"$in": ids}})
    await topups.topups_col().insert_many([dict(lost), dict(fine), dict(fresh)])
    try:
        await credits.add_bought(ws_id, topup_id=fine["id"], credits=100, label="100 credits")  # this one arrived; only its mark is missing
        before = (await credits.bought_left(pool.id))[0]
        assert before == 100
        assert await topups.finish_unfinished_packs() == 1
        assert (await credits.bought_left(pool.id))[0] == 600, "only the lost pack's 500 were added"
        assert (await topups.topups_col().find_one({"id": lost["id"]})).get("credited_at")
        assert (await topups.topups_col().find_one({"id": fine["id"]})).get("credited_at"), "the pack that arrived is marked, not credited twice"
        assert not (await topups.topups_col().find_one({"id": fresh["id"]})).get("credited_at"), "a pack paid a moment ago may still be in progress"
        assert await topups.finish_unfinished_packs() == 0, "running it again changes nothing"
    finally:
        await topups.topups_col().delete_many({"id": {"$in": ids}})
