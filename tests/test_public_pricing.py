"""The website's pricing page reads plans, limits and prices from what Ops saved, without signing in."""
from __future__ import annotations

from app.billing import prices as PR
from app.db.mongo import get_db, users


async def test_anyone_reads_every_plan_with_the_prices_ops_saved(signup_user, make_client):
    visitor = make_client()
    res = await visitor.get("/api/v1/public/pricing")
    assert res.status_code == 200
    body = res.json()
    assert set(body["plans"]) == {"free", "single", "duo", "large"}
    assert body["plans"]["free"]["limits"]["credits_month"] is not None
    assert body["signup_mode"] in ("open", "invite") and isinstance(body["checkout_open"], bool)
    assert {r["plan"] for r in body["list_prices"]["INR"]} == {"single", "duo", "large"}
    assert "customer" not in res.text and "key_id" not in res.text

    ops, profile = await signup_user("Prices")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    try:
        book = (await ops.get("/api/v1/ops/billing/prices")).json()["book"]
        book["monthly"]["duo"]["INR"] = 249_900
        assert (await ops.put("/api/v1/ops/billing/prices", json={"book": book, "reason": "Website check"})).status_code == 200
        body = (await visitor.get("/api/v1/public/pricing")).json()
        duo = next(r for r in body["list_prices"]["INR"] if r["plan"] == "duo")
        assert duo["monthly"]["amount"] == 249_900
    finally:
        await get_db()["billing_settings"].delete_one({"_id": "prices"})
        await PR.load_book(force=True)
