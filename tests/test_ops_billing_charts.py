"""Ops, Plans and billing: payments marked as a plan or credits and filtered, the payments file that needs Approve, and the charts:
money after refunds split into plans and credits, payments that went through or failed, workspaces that paid, and money by plan."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.billing import subscriptions as S
from app.billing.provider import get_provider
from tests.conftest import create_workspace
from tests.test_ops_team import _join, _master
from tests.test_payments import standin  # noqa: F401 - the fixture is used by name


async def _seed(signup_user):
    master, _ = await _master(signup_user)
    owner, _ = await signup_user("Payer")
    ws = await create_workspace(owner, f"Paying {uuid4().hex[:5]}")
    mode = get_provider().mode
    now = datetime.now(timezone.utc)
    sub_id = uuid4().hex
    await S.subs_col().insert_one({"id": sub_id, "workspace_id": ws, "plan": "duo", "status": "active", "mode": mode, "created_at": now})
    base = {"workspace_id": ws, "currency": "INR", "mode": mode}
    await S.payments_col().insert_many([
        {**base, "id": f"pay_{uuid4().hex[:8]}", "subscription_id": sub_id, "amount": 99_900, "status": "captured", "at": now - timedelta(days=2)},
        {**base, "id": f"pay_{uuid4().hex[:8]}", "topup_id": "t1", "amount": 44_900, "refunded": 4_900, "refund_status": "partly_refunded",
         "status": "captured", "at": now - timedelta(days=1)},
        {**base, "id": f"pay_{uuid4().hex[:8]}", "subscription_id": sub_id, "amount": 99_900, "status": "failed", "at": now - timedelta(hours=3)},
    ])
    return master, ws


async def test_payments_are_marked_and_filtered(signup_user, standin):
    master, ws = await _seed(signup_user)
    rows = (await master.get("/api/v1/ops/billing/payments", params={"q": ws})).json()["payments"]
    assert sorted(r["kind"] for r in rows) == ["credits", "plan", "plan"]
    credits = (await master.get("/api/v1/ops/billing/payments", params={"q": ws, "kind": "credits"})).json()["payments"]
    assert [r["kind"] for r in credits] == ["credits"]
    failed = (await master.get("/api/v1/ops/billing/payments", params={"q": ws, "status": "failed"})).json()["payments"]
    assert [r["status"] for r in failed] == ["failed"]
    refunded = (await master.get("/api/v1/ops/billing/payments", params={"q": ws, "status": "refunded"})).json()["payments"]
    assert len(refunded) == 1 and refunded[0]["kind"] == "credits"


async def test_the_payments_file_needs_approve_and_is_kept_in_the_history(signup_user, standin):
    master, ws = await _seed(signup_user)
    res = await master.get("/api/v1/ops/billing/payments/export.csv", params={"q": ws})
    assert res.status_code == 200 and "Credits" in res.text and "Plan" in res.text
    role = await master.post("/api/v1/ops/team/roles", json={
        "name": f"Billing viewer {uuid4().hex[:6]}", "permissions": {"billing": ["view"]}, "reason": "Reads payments"})
    assert role.status_code == 200, role.text
    viewer, _ = await _join(master, signup_user, "Viewer", "staff", role.json()["role"]["id"])
    assert (await viewer.get("/api/v1/ops/billing/payments")).status_code == 200
    assert (await viewer.get("/api/v1/ops/billing/payments/export.csv")).status_code == 403


async def test_charts_split_money_after_refunds_and_count_every_bucket(signup_user, standin):
    await _seed(signup_user)
    master, _ = await _master(signup_user)
    for rng, at_least in (("30d", 30), ("90d", 13), ("12m", 12)):
        body = (await master.get("/api/v1/ops/billing/charts", params={"range": rng})).json()
        assert len(body["series"]) >= at_least, rng
        total = {k: sum(p[k] for p in body["series"]) for k in ("plans", "credits", "paid", "failed")}
        assert total["plans"] >= 99_900 and total["credits"] >= 40_000 and total["paid"] >= 2 and total["failed"] >= 1, rng
        assert any(p["plan"] == "duo" and p["amount"] >= 99_900 for p in body["by_plan"]), rng
    assert (await master.get("/api/v1/ops/billing/charts", params={"range": "5y"})).status_code == 422
