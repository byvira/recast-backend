"""Self-healing connections (app.pipelines.publish.health + token_refresh) and
fixed-age metric checkpoints (app.pipelines.analytics.checkpoints).

The escalation policy under test: one failed automatic recovery is logged and
retried quietly; only the second consecutive failure escalates (Odette flag in
the admins' Active lane + owner email), exactly once; any recovery resolves it.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.mongo import (
    activity_entries,
    content_pieces,
    post_metric_checkpoints,
    workspace_connections,
    workspace_flags,
)
from app.pipelines.analytics import checkpoints
from app.pipelines.analytics.base import PostMetrics
from app.pipelines.publish import health
from app.pipelines.publish.token_store import save_token
from app.workers import token_refresh
from tests.conftest import create_workspace, signup_new_user


@pytest.fixture(autouse=True)
def _no_external_calls(monkeypatch):
    """Odette's localized summary (LLM on cache miss), ops alerts and emails
    are side effects, not what's under test."""
    import app.agents.supervisor.personas as personas
    import app.pipelines.publish.supervisor.alerts as alerts

    sent: list[str] = []

    async def _summary(flag_type, detail, language="en"):
        return f"{detail.get('platform')} needs reconnecting ({detail.get('failures')} failures)."

    async def _alert(**kwargs):
        sent.append("ops")

    async def _email(template, to, *args, **kwargs):
        sent.append(template)
        return True

    monkeypatch.setattr(personas, "odette_flag_summary", _summary)
    monkeypatch.setattr(alerts, "alert_token_refresh_failure", _alert)
    monkeypatch.setattr(health, "send_templated_email", _email)
    return sent


async def _connection(ws_id: str, platform: str = "linkedin", **extra) -> None:
    await workspace_connections.insert_one({
        "id": str(uuid4()), "workspace_id": ws_id, "platform": platform,
        "access_token": "x", "refresh_token": None, "is_active": True,
        "username": "acme", "expires_at": datetime.now(timezone.utc) + timedelta(days=2),
        **extra,
    })


async def _open_flags(ws_id: str) -> list[dict]:
    return await workspace_flags.find(
        {"workspace_id": ws_id, "flag_type": "connection_broken", "status": "open"}
    ).to_list(10)


async def test_first_failure_is_quiet_second_escalates_once(api_client, _no_external_calls):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Heal", tier="large")
    await _connection(ws_id)

    assert await health.record_failure(ws_id, "linkedin", reason="invalid_grant") == 1
    assert await _open_flags(ws_id) == []
    assert _no_external_calls == []          # nobody bothered yet
    conn = await workspace_connections.find_one({"workspace_id": ws_id})
    assert conn["health"]["state"] == "degraded"

    assert await health.record_failure(ws_id, "linkedin", reason="invalid_grant") == 2
    flags = await _open_flags(ws_id)
    assert len(flags) == 1
    assert sorted(_no_external_calls) == ["ops", "platform-reconnect-needed"]
    # Odette's flag is in the admins' Active lane.
    row = await activity_entries.find_one({"_id": f"odette_flag:{flags[0]['_id']}"})
    assert row["lane"] == "active" and row["visibility"] == "admins"

    # Third failure: still counted, but no second flag / email.
    await health.record_failure(ws_id, "linkedin", reason="invalid_grant")
    assert len(await _open_flags(ws_id)) == 1
    assert len(_no_external_calls) == 2


async def test_reconnect_recovers_and_resolves_the_flag(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Recover", tier="large")
    await _connection(ws_id)
    await health.record_failure(ws_id, "linkedin", reason="revoked", broken=True)
    await health.record_failure(ws_id, "linkedin", reason="revoked", broken=True)
    [flag] = await _open_flags(ws_id)

    await save_token(
        workspace_id=ws_id, platform="linkedin", access_token="new", refresh_token=None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=60),
        platform_user_id="u1", username="acme",
    )

    conn = await workspace_connections.find_one({"workspace_id": ws_id})
    assert conn["health"]["state"] == "healthy" and conn["health"]["failures"] == 0
    assert await _open_flags(ws_id) == []
    row = await activity_entries.find_one({"_id": f"odette_flag:{flag['_id']}"})
    assert row["lane"] == "passive"
    assert row["decision"] is None                       # system-resolved, not a human decision
    assert row["metadata"]["decision"] == "Resolved automatically"
    recovered = await activity_entries.find_one(
        {"workspace_id": ws_id, "title": "Linkedin connection recovered"}
    )
    assert recovered is not None


async def test_mark_healthy_is_a_noop_when_already_healthy(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Quiet OK", tier="large")
    await _connection(ws_id, health={"state": "healthy", "failures": 0})
    await health.mark_healthy(ws_id, "linkedin", via="a successful publish")
    assert await activity_entries.count_documents({"workspace_id": ws_id}) == 0


def test_refresh_due_policy():
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=2)
    fresh = {"expires_at": soon, "last_refreshed_at": now - timedelta(hours=2)}
    assert token_refresh._is_due(fresh, now) is False                      # healthy, tried recently
    assert token_refresh._is_due({**fresh, "last_refreshed_at": now - timedelta(hours=21)}, now)
    failing = {**fresh, "health": {"failures": 1, "checked_at": now - timedelta(minutes=56)}}
    assert token_refresh._is_due(failing, now)                             # quick retry
    escalated = {**fresh, "health": {"failures": 3, "escalated": True,
                                     "checked_at": now - timedelta(hours=2)}}
    assert token_refresh._is_due(escalated, now) is False                  # quieter cadence
    assert token_refresh._is_due({"expires_at": now + timedelta(days=30)}, now) is False


# ─────────────────────────────────────────────────────────────────────────────
# Metric checkpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_due_checkpoints_never_backfill_a_missed_window():
    assert checkpoints.due_checkpoints(timedelta(minutes=30), set()) == (None, [])
    assert checkpoints.due_checkpoints(timedelta(hours=2), set()) == ("1h", [])
    # Job was down through the 1h window: 1h is missed, 24h captured now.
    assert checkpoints.due_checkpoints(timedelta(hours=25), set()) == ("24h", ["1h"])
    assert checkpoints.due_checkpoints(timedelta(hours=25), {"1h", "24h"}) == (None, [])
    assert checkpoints.due_checkpoints(timedelta(days=9), {"1h", "24h", "72h"}) == (None, ["7d"])


async def test_capture_records_one_checkpoint_at_its_real_age(api_client, monkeypatch):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Checkpoints", tier="large")
    await _connection(ws_id)
    piece_id = str(uuid4())
    await content_pieces.insert_one({
        "piece_id": piece_id, "workspace_id": ws_id, "user_id": "u1", "brand_id": "b1",
        "platform": "LinkedIn", "publish_target": "linkedin", "platform_post_id": "urn:1",
        "publish_status": "published", "word_count": 120,
        "published_at": datetime.now(timezone.utc) - timedelta(hours=25),
    })

    async def _fake_fetch(workspace_id, posts):
        return [PostMetrics(workspace_id=workspace_id, platform="linkedin", post_id=p["piece_id"],
                            platform_post_id=p["platform_post_id"], likes=12, comments=3,
                            reach=400, engagement_rate=3.75) for p in posts]

    monkeypatch.setattr(checkpoints, "fetch_post_metrics_all", _fake_fetch)
    await checkpoints._capture_workspace(ws_id, datetime.now(timezone.utc))

    rows = await post_metric_checkpoints.find({"piece_id": piece_id}).to_list(5)
    assert [r["checkpoint"] for r in rows] == ["24h"]
    assert rows[0]["metrics"]["likes"] == 12
    assert 24.9 <= rows[0]["age_hours"] <= 25.2
    piece = await content_pieces.find_one({"piece_id": piece_id})
    assert piece["metric_checkpoints"] == {"1h": "missed", "24h": "captured"}

    # Same tick again: nothing new is due.
    await checkpoints._capture_workspace(ws_id, datetime.now(timezone.utc))
    assert await post_metric_checkpoints.count_documents({"piece_id": piece_id}) == 1
