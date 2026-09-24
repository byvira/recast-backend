"""Tests for the analytics daily snapshot + real week-over-week delta —
the backend half of the Home page's Insights Strip.

Both account_metrics and post_metrics are upserted in place (see
app.pipelines.analytics.aggregator) — neither retains history, so there
was no way to diff "now" against "7 days ago" before this. These tests
insert account_metrics directly via Mongo (real external platform calls
require OAuth tokens not available in tests, same constraint every other
analytics-adjacent test in this codebase works around), exercising
record_daily_snapshot()/get_previous_totals()/compute_deltas() and the
real GET /api/v1/analytics/summary route.
"""

from datetime import datetime, timedelta, timezone

from app.db.mongo import account_metrics, analytics_daily_snapshots
from app.pipelines.analytics.snapshots import (
    compute_deltas,
    get_previous_totals,
    record_daily_snapshot,
)
from tests.conftest import create_workspace, signup_new_user


async def _seed_account_metrics(ws_id: str, *, reach: int, impressions: int, followers: int = 100) -> None:
    await account_metrics.update_one(
        {"workspace_id": ws_id, "platform": "linkedin"},
        {"$set": {
            "workspace_id": ws_id,
            "platform": "linkedin",
            "platform_user_id": "u1",
            "followers": followers,
            "total_impressions": impressions,
            "total_reach": reach,
            "total_posts": 3,
            "fetched_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


async def test_record_daily_snapshot_persists_todays_totals(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Analytics WS")

    await _seed_account_metrics(ws_id, reach=1000, impressions=2000)
    await record_daily_snapshot(ws_id)

    today = datetime.now(timezone.utc).date().isoformat()
    doc = await analytics_daily_snapshots.find_one({"workspace_id": ws_id, "date": today})
    assert doc is not None
    assert doc["totals"]["reach"] == 1000
    assert doc["totals"]["impressions"] == 2000


async def test_record_daily_snapshot_is_idempotent_same_day(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Analytics WS")

    await _seed_account_metrics(ws_id, reach=1000, impressions=2000)
    await record_daily_snapshot(ws_id)

    await _seed_account_metrics(ws_id, reach=1500, impressions=2500)
    await record_daily_snapshot(ws_id)

    today = datetime.now(timezone.utc).date().isoformat()
    count = await analytics_daily_snapshots.count_documents({"workspace_id": ws_id, "date": today})
    assert count == 1  # updated in place, not duplicated

    doc = await analytics_daily_snapshots.find_one({"workspace_id": ws_id, "date": today})
    assert doc["totals"]["reach"] == 1500  # reflects the latest run


async def test_get_previous_totals_finds_nearest_snapshot_at_or_before_target(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Analytics WS")

    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).date().isoformat()
    await analytics_daily_snapshots.insert_one(
        {"workspace_id": ws_id, "date": ten_days_ago, "totals": {"reach": 100}},
    )
    await analytics_daily_snapshots.insert_one(
        {"workspace_id": ws_id, "date": eight_days_ago, "totals": {"reach": 200}},
    )

    previous = await get_previous_totals(ws_id, days_ago=7)
    assert previous == {"reach": 200}  # the nearer of the two, still at-or-before 7 days ago


async def test_get_previous_totals_none_when_no_snapshot_old_enough(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Analytics WS")

    today = datetime.now(timezone.utc).date().isoformat()
    await analytics_daily_snapshots.insert_one(
        {"workspace_id": ws_id, "date": today, "totals": {"reach": 100}},
    )

    assert await get_previous_totals(ws_id, days_ago=7) is None


def test_compute_deltas_computes_real_percentage_change():
    current = {"reach": 1200, "impressions": 500}
    previous = {"reach": 1000, "impressions": 0}
    deltas = compute_deltas(current, previous)
    assert deltas["reach"] == 20.0
    assert deltas["impressions"] is None  # can't compute % change from a zero baseline


def test_compute_deltas_none_when_no_previous_totals():
    assert compute_deltas({"reach": 1000}, None) is None


async def test_summary_endpoint_includes_null_deltas_when_no_snapshot_exists(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Analytics WS")

    await _seed_account_metrics(ws_id, reach=1000, impressions=2000)

    res = await api_client.get("/api/v1/analytics/summary", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["totals"]["reach"] == 1000
    assert body["previous_totals"] is None
    assert body["deltas"] is None


async def test_summary_endpoint_includes_real_delta_when_week_old_snapshot_exists(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Analytics WS")

    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).date().isoformat()
    await analytics_daily_snapshots.insert_one(
        {"workspace_id": ws_id, "date": eight_days_ago, "totals": {
            "followers": 100, "impressions": 1000, "reach": 500,
            "likes": 10, "comments": 2, "shares": 1,
        }},
    )
    await _seed_account_metrics(ws_id, reach=750, impressions=1000, followers=100)

    res = await api_client.get("/api/v1/analytics/summary", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["previous_totals"]["reach"] == 500
    assert body["deltas"]["reach"] == 50.0  # (750 - 500) / 500 * 100


# ─────────────────────────────────────────────────────────────────────────────
# Top performers (Library "Top Performers (Top 5%)" tab, PAR-013)
# ─────────────────────────────────────────────────────────────────────────────

async def test_top_performers_ranks_top_five_percent_at_24h(api_client):
    from uuid import uuid4 as _uuid4

    from app.db.mongo import post_metric_checkpoints
    from tests.conftest import create_workspace, signup_new_user

    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Top Performers", tier="large")
    h = {"X-Workspace-Id": ws_id}

    empty = (await api_client.get("/api/v1/analytics/top-performers", headers=h)).json()
    assert empty["measured"] == 0 and empty["items"] == []

    ids = []
    for rate in [1.0, 2.0, 9.5, 3.0] + [0.5] * 17:          # 21 measured → ceil(5%) = 2
        pid = str(_uuid4())
        ids.append((pid, rate))
        await post_metric_checkpoints.insert_one({
            "_id": f"{pid}:24h", "workspace_id": ws_id, "piece_id": pid,
            "checkpoint": "24h", "metrics": {"engagement_rate": rate},
        })
    # A 7d checkpoint must not be mixed into the 24h ranking.
    await post_metric_checkpoints.insert_one({
        "_id": "other:7d", "workspace_id": ws_id, "piece_id": "other",
        "checkpoint": "7d", "metrics": {"engagement_rate": 99.0},
    })

    res = (await api_client.get("/api/v1/analytics/top-performers", headers=h)).json()
    assert res["measured"] == 21
    by_rate = {pid: rate for pid, rate in ids}
    assert [by_rate[i["piece_id"]] for i in res["items"]] == [9.5, 3.0]
