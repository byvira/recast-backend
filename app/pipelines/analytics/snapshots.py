"""Daily analytics snapshots — the real week-over-week delta behind the
Home page's Insights Strip.

Both `account_metrics` and `post_metrics` are upserted in place
(app.pipelines.analytics.aggregator) — neither collection retains
history, so there is no stored point-in-time value to diff "now" against
"7 days ago" from either as-is. This module adds a small daily snapshot
of the same `summarize()` totals every workspace already computes on
every /analytics/summary call, recorded once per day (keyed by
workspace_id + date, so same-day reruns of the scheduled refresh just
update that day's row rather than duplicating).

Until a workspace has at least one snapshot ~7 days old, get_previous_totals()
returns None and the caller shows an honest "not enough data yet" gap
rather than inventing a percentage.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db.mongo import account_metrics as account_metrics_col
from app.db.mongo import analytics_daily_snapshots
from app.db.mongo import post_metrics as post_metrics_col
from app.pipelines.analytics.aggregator import summarize
from app.pipelines.analytics.base import AccountMetrics, PostMetrics


async def _current_totals(workspace_id: str) -> dict:
    """Same read + summarize() GET /analytics/summary itself does."""
    account_docs = await account_metrics_col.find(
        {"workspace_id": workspace_id}
    ).to_list(length=20)
    post_docs = await post_metrics_col.find(
        {"workspace_id": workspace_id}, sort=[("fetched_at", -1)]
    ).to_list(length=100)
    account_metrics = [AccountMetrics(**d) for d in account_docs]
    post_metrics = [PostMetrics(**d) for d in post_docs]
    return summarize(account_metrics, post_metrics)["totals"]


async def record_daily_snapshot(workspace_id: str) -> None:
    """Upsert today's totals snapshot for this workspace. Called by the
    existing refresh_analytics scheduled job (every 6 hours) — cheap and
    idempotent per day."""
    totals = await _current_totals(workspace_id)
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc)
    await analytics_daily_snapshots.update_one(
        {"workspace_id": workspace_id, "date": today},
        {
            "$set": {"totals": totals, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def get_previous_totals(workspace_id: str, days_ago: int = 7) -> Optional[dict]:
    """The most recent snapshot at or before `days_ago` days back — not an
    exact-date match, so a gap in the scheduler's run history still finds
    a usable baseline. None if no snapshot exists that far back yet."""
    target_date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    doc = await analytics_daily_snapshots.find_one(
        {"workspace_id": workspace_id, "date": {"$lte": target_date}},
        sort=[("date", -1)],
    )
    return doc["totals"] if doc else None


def compute_deltas(current_totals: dict, previous_totals: Optional[dict]) -> Optional[dict]:
    """Percentage change per metric, current vs previous. None (the whole
    dict) when there's no baseline yet; an individual metric's delta is
    None when the previous value was 0 (a % change from zero is undefined,
    not honestly representable as a number)."""
    if previous_totals is None:
        return None
    deltas: dict[str, Optional[float]] = {}
    for key, current_value in current_totals.items():
        previous_value = previous_totals.get(key, 0)
        if not previous_value:
            deltas[key] = None
        else:
            deltas[key] = round((current_value - previous_value) / previous_value * 100, 1)
    return deltas
